"""A layer that samples the next tokens from the model's outputs."""
import itertools
from math import inf
from typing import Dict, List, Optional, Tuple

import torch
import torch.nn as nn

from aphrodite.common.sampling_params import SamplingType
from aphrodite.common.sequence import (CompletionSequenceGroupOutput, Logprob,
                                       PromptLogprobs, SampleLogprobs,
                                       SamplerOutput, SequenceOutput)
from aphrodite.triton_utils import HAS_TRITON

if HAS_TRITON:
    from aphrodite.modeling.layers.ops.sample import sample as sample_triton

from aphrodite.modeling.sampling_metadata import (SamplingMetadata,
                                                  SamplingTensors,
                                                  SequenceGroupToSample)

# (num_token_ids, num_parent_ids) per sequence group.
SampleResultType = List[Tuple[List[int], List[int]]]


class Sampler(nn.Module):
    """Samples the next tokens from the model's outputs.

    This layer does the following:
    1. Discard the hidden states that are not used for sampling (i.e., all
        tokens except the final one in each prompt).
    2. Compute the logits for the next tokens.
    3. Apply presence, frequency and repetition penalties.
    4. Apply temperature scaling.
    5. Apply top-p and top-k truncation.
    6. Sample the next tokens.
    Here, each sequence group within the batch can have different sampling
    parameters (e.g., sampling method, temperature, top-p, top-k, etc.).

    The structure of the logits tensor is coupled with the seq_groups in
    sampling_metadata. Typically, each sequence in each seq_group has one row in
    logits for the next token to be sampled; however, for a seq_group with a
    prompt request with the prompt_logprobs sampling parameter, there are rows
    in logits for each token in the input prompt.
    """

    def __init__(self):
        super().__init__()

        # Whether or not the SamplerOutput should have on-device tensors
        # containing the sampled token ids and probabilities. This is used by
        # speculative decoding.
        self.include_gpu_probs_tensor = False

    def _init_sampling_tensors(
        self,
        logits: torch.Tensor,
        sampling_metadata: SamplingMetadata,
    ):
        """The goal here is to reuse sampling tensors between similar decode
        runs. This is possible because sampling logic does not change between
        decodes of the same sequences.
        """
        _, vocab_size = logits.shape

        # First free any existing stored sampling tensors.
        # This is necessary because some sampling tensors may
        # have pinned memory.
        self._sampling_tensors = None

        # Initialize new sampling tensors
        (sampling_tensors, do_penalties, do_top_p_top_k, do_top_as, do_min_p,
         do_tfss, do_eta_cutoffs, do_epsilon_cutoffs, do_typical_ps,
         do_quadratic) = SamplingTensors.from_sampling_metadata(
             sampling_metadata, vocab_size, logits.device, logits.dtype)

        self._sampling_tensors = sampling_tensors
        self._do_penalties = do_penalties
        self._do_top_p_top_k = do_top_p_top_k
        self._do_top_as = do_top_as
        self._do_min_p = do_min_p
        self._do_tfss = do_tfss
        self._do_eta_cutoffs = do_eta_cutoffs
        self._do_epsilon_cutoffs = do_epsilon_cutoffs
        self._do_typical_ps = do_typical_ps
        self._do_quadratic = do_quadratic

    def forward(
        self,
        logits: torch.Tensor,
        sampling_metadata: SamplingMetadata,
    ) -> Optional[SamplerOutput]:
        """
        Args:
            logits: (num_tokens, vocab_size).
            sampling_metadata: Metadata for sampling.
        """
        assert logits is not None
        _, vocab_size = logits.shape

        # Prepare sampling tensors with pinned memory to avoid blocking.
        if not sampling_metadata.reuse_sampling_tensors:
            self._init_sampling_tensors(logits, sampling_metadata)
        elif self._do_penalties:
            # In this case, the sampling tensors logic depends on
            # "output_tokens" of a sequence. As a result, we cannot
            # reuse sampling tensors, since "output_tokens" changes
            # between decode runs.
            self._init_sampling_tensors(logits, sampling_metadata)

        assert self._sampling_tensors is not None
        sampling_tensors = self._sampling_tensors
        do_penalties = self._do_penalties
        do_top_p_top_k = self._do_top_p_top_k
        do_top_as = self._do_top_as
        do_min_p = self._do_min_p
        do_tfss = self._do_tfss
        do_eta_cutoffs = self._do_eta_cutoffs
        do_epsilon_cutoffs = self._do_epsilon_cutoffs
        do_typical_ps = self._do_typical_ps
        do_quadratic = self._do_quadratic

        logits = _apply_min_tokens_penalty(logits, sampling_metadata)

        # Apply presence and frequency penalties.
        if do_penalties:
            logits = _apply_penalties(logits, sampling_tensors.prompt_tokens,
                                      sampling_tensors.output_tokens,
                                      sampling_tensors.presence_penalties,
                                      sampling_tensors.frequency_penalties,
                                      sampling_tensors.repetition_penalties)

        # Apply temperature scaling.
        # Use in-place division to avoid creating a new tensor.
        logits.div_(sampling_tensors.temperatures.unsqueeze(dim=1))

        if do_top_p_top_k:
            logits = _apply_top_k_top_p(logits, sampling_tensors.top_ps,
                                        sampling_tensors.top_ks)

        if do_top_as:
            logits = _apply_top_a(logits, sampling_tensors.top_as)

        if do_min_p:
            logits = _apply_min_p(logits, sampling_tensors.min_ps)

        if do_tfss:
            logits = _apply_tfs(logits, sampling_tensors.tfss)

        if do_eta_cutoffs:
            logits = _apply_eta_cutoff(logits, sampling_tensors.eta_cutoffs)

        if do_epsilon_cutoffs:
            logits = _apply_epsilon_cutoff(logits,
                                           sampling_tensors.epsilon_cutoffs)

        if do_typical_ps:
            logits = _apply_typical_sampling(logits,
                                             sampling_tensors.typical_ps)

        if do_quadratic:
            logits = _apply_quadratic_sampling(
                logits, sampling_tensors.smoothing_factors,
                sampling_tensors.smoothing_curves)

        # banned_tokens = _get_custom_token_bans(sampling_metadata)
        # logits = _apply_token_bans(logits, banned_tokens)

        # We use float32 for probabilities and log probabilities.
        # Compute the probabilities.
        probs = torch.softmax(logits, dim=-1, dtype=torch.float)
        # Compute the log probabilities.
        logprobs = torch.log_softmax(logits, dim=-1, dtype=torch.float)

        # Sample the next tokens.
        sample_results, maybe_sampled_tokens_tensor = _sample(
            probs,
            logprobs,
            sampling_metadata,
            sampling_tensors,
            include_gpu_probs_tensor=self.include_gpu_probs_tensor,
            modify_greedy_probs=self._should_modify_greedy_probs_inplace,
        )

        if self.include_gpu_probs_tensor:
            assert maybe_sampled_tokens_tensor is not None
            on_device_tensors = (probs, logprobs, maybe_sampled_tokens_tensor)
        else:
            on_device_tensors = None

        # Get the logprobs query results.
        prompt_logprobs = None
        sample_logprobs = None
        if not sampling_metadata.skip_sampler_cpu_output:
            prompt_logprobs, sample_logprobs = _get_logprobs(
                logprobs, sampling_metadata, sample_results)

        return _build_sampler_output(
            sample_results,
            sampling_metadata,
            prompt_logprobs,
            sample_logprobs,
            on_device_tensors=on_device_tensors,
            skip_sampler_cpu_output=sampling_metadata.skip_sampler_cpu_output)

    @property
    def _should_modify_greedy_probs_inplace(self) -> bool:
        """Whether or not the sampler should modify the probability distribution
        of greedily-sampled tokens such that multinomial sampling would sample
        the greedily-sampled token.

        In other words, if True then we set the probability of the greedily-
        sampled token to 1.

        This is used by speculative decoding, which requires that the sampling
        method be encoded into the probability distribution.
        """
        # Modify greedy probs if include_gpu_probs_tensor is set.
        return self.include_gpu_probs_tensor


def _get_bin_counts_and_mask(
    tokens: torch.Tensor,
    vocab_size: int,
    num_seqs: int,
) -> Tuple[torch.Tensor, torch.Tensor]:
    # Compute the bin counts for the tokens.
    # vocab_size + 1 for padding.
    bin_counts = torch.zeros((num_seqs, vocab_size + 1),
                             dtype=torch.long,
                             device=tokens.device)
    bin_counts.scatter_add_(1, tokens, torch.ones_like(tokens))
    bin_counts = bin_counts[:, :vocab_size]
    mask = bin_counts > 0

    return bin_counts, mask


def _get_custom_token_bans(
        sampling_metadata: SamplingMetadata) -> List[List[int]]:
    assert sampling_metadata.seq_groups is not None
    banned_tokens: List[List[int]] = []
    for i, seq_group in enumerate(sampling_metadata.seq_groups):
        sampling_params = sampling_metadata.seq_groups[i].sampling_params
        seq_ids = seq_group.seq_ids
        custom_token_bans = sampling_params.custom_token_bans
        if (i < sampling_metadata.num_prompts
                and sampling_params.prompt_logprobs is not None):
            prompt_len = len(seq_group.prompt_logprob_indices)
            banned_tokens += [custom_token_bans] * (prompt_len - 1)
        banned_tokens += [custom_token_bans] * len(seq_ids)
    return banned_tokens


def _apply_penalties(logits: torch.Tensor, prompt_tokens_tensor: torch.Tensor,
                     output_tokens_tensor: torch.Tensor,
                     presence_penalties: torch.Tensor,
                     frequency_penalties: torch.Tensor,
                     repetition_penalties: torch.Tensor) -> torch.Tensor:
    num_seqs, vocab_size = logits.shape
    _, prompt_mask = _get_bin_counts_and_mask(prompt_tokens_tensor, vocab_size,
                                              num_seqs)
    output_bin_counts, output_mask = _get_bin_counts_and_mask(
        output_tokens_tensor, vocab_size, num_seqs)

    repetition_penalties = repetition_penalties[:, None].repeat(1, vocab_size)
    repetition_penalties[~(prompt_mask | output_mask)] = 1.0
    logits = torch.where(logits > 0, logits / repetition_penalties,
                         logits * repetition_penalties)

    # We follow the definition in OpenAI API.
    # Refer to https://platform.openai.com/docs/api-reference/parameter-details
    logits -= frequency_penalties.unsqueeze_(dim=1) * output_bin_counts
    logits -= presence_penalties.unsqueeze_(dim=1) * output_mask
    return logits


def _apply_token_bans(logits: torch.Tensor,
                      banned_tokens: List[List[int]]) -> torch.Tensor:
    for i, banned_token_ids in enumerate(banned_tokens):
        if not banned_token_ids:
            continue
        logits[i, banned_token_ids] = -float("inf")
    return logits


def _apply_min_tokens_penalty(
    logits: torch.Tensor,
    sampling_metadata: SamplingMetadata,
) -> torch.Tensor:
    """Apply min_tokens penalty which sets stop tokens to -inf if min_tokens
        have not been generated yet
    """
    # list of indices in logits that will be set to -inf
    logits_to_penalize = []
    logits_applied = 0
    for seq_group in sampling_metadata.seq_groups:
        seq_ids = seq_group.seq_ids
        sampling_params = seq_group.sampling_params

        sample_indices = seq_group.sample_indices
        logits_applied += len(sample_indices) + len(
            seq_group.prompt_logprob_indices)
        if not seq_group.do_sample:
            continue

        start_idx = sample_indices[0]
        min_tokens = sampling_params.min_tokens
        token_ids_to_penalize = sampling_params.all_stop_token_ids
        if min_tokens > 0 and token_ids_to_penalize:
            seqs_to_penalize = []
            for j, seq_id in enumerate(seq_ids):
                seq_data = seq_group.seq_data[seq_id]
                if len(seq_data.output_token_ids_array) < min_tokens:
                    seqs_to_penalize.append(j)

            if seqs_to_penalize:
                # convert to the index into logits
                seqs_to_penalize = [start_idx + j for j in seqs_to_penalize]
                # itertools.product pairs each seq index with every token id
                logits_to_penalize.extend(
                    itertools.product(seqs_to_penalize, token_ids_to_penalize))

    if logits_to_penalize:
        # use zip and * to group indices along each dimension
        # eg. [ (1,2), (1,3), (5,6) ] -> ( (1,1,5), (2,3,6) )
        logits[tuple(zip(*logits_to_penalize))] = -float("inf")

    # verifies that no rows in logits were missed unexpectedly
    assert logits_applied == logits.shape[0]
    return logits


def _apply_top_k_top_p(
    logits: torch.Tensor,
    p: torch.Tensor,
    k: torch.Tensor,
) -> torch.Tensor:
    logits_sort, logits_idx = logits.sort(dim=-1, descending=False)

    # Apply top-k.
    top_k_mask = logits_sort.size(1) - k.to(torch.long)
    # Get all the top_k values.
    top_k_mask = logits_sort.gather(1, top_k_mask.unsqueeze(dim=1))
    top_k_mask = logits_sort < top_k_mask
    logits_sort.masked_fill_(top_k_mask, -float("inf"))

    # Apply top-p.
    probs_sort = logits_sort.softmax(dim=-1)
    probs_sum = probs_sort.cumsum(dim=-1)
    top_p_mask = probs_sum <= 1 - p.unsqueeze(dim=1)
    # at least one
    top_p_mask[:, -1] = False
    logits_sort.masked_fill_(top_p_mask, -float("inf"))

    # Re-sort the probabilities.
    src = torch.arange(logits_idx.shape[-1],
                       device=logits_idx.device).expand_as(logits_idx)
    logits_idx_inv = torch.empty_like(logits_idx).scatter_(dim=-1,
                                                           index=logits_idx,
                                                           src=src)
    logits = torch.gather(logits_sort, dim=-1, index=logits_idx_inv)
    return logits


def _apply_min_p(
    logits: torch.Tensor,
    min_p: torch.Tensor,
) -> torch.Tensor:
    """
    Adapted from
    https://github.com/oobabooga/text-generation-webui/blob/3146124ec01f02c8fb1650a6517cf1b60b537aaf/modules/sampler_hijack.py#L16C17-L16C17
    """
    probs = torch.softmax(logits, dim=-1)
    top_probs, _ = probs.max(dim=-1, keepdim=True)
    scaled_min_p = min_p.unsqueeze_(dim=1) * top_probs
    tokens_to_remove = probs < scaled_min_p
    logits = logits.masked_fill_(tokens_to_remove, -float("inf"))

    return logits


def _apply_top_a(
    logits: torch.Tensor,
    top_a: torch.Tensor,
) -> torch.Tensor:
    probs = torch.softmax(logits, dim=-1)
    top_probs, _ = probs.max(dim=-1, keepdim=True)
    threshold = torch.pow(top_probs, 2) * top_a.unsqueeze_(dim=1)
    tokens_to_remove = probs < threshold
    logits = logits.masked_fill_(tokens_to_remove, -float("inf"))

    return logits


def _apply_tfs(
    logits: torch.Tensor,
    tfs: torch.Tensor,
) -> torch.Tensor:
    logits_sort, logits_idx = logits.sort(dim=-1, descending=True)
    d2 = logits_sort.softmax(dim=-1).diff().diff().abs()
    normalized_d2 = d2 / torch.sum(d2, dim=-1, keepdim=True)
    curvature_cdf = torch.cumsum(normalized_d2, dim=-1)

    tfs_mask = curvature_cdf > tfs.unsqueeze(dim=-1)

    tfs_mask = torch.cat(
        (
            torch.zeros(
                logits.shape[0], 1, dtype=torch.bool, device=logits.device),
            tfs_mask,
            torch.ones(
                logits.shape[0], 1, dtype=torch.bool, device=logits.device),
        ),
        dim=-1,
    )

    logits_sort[tfs_mask] = -float("inf")
    logits = torch.gather(logits_sort,
                          dim=-1,
                          index=torch.argsort(logits_idx, dim=-1))

    return logits


def _apply_eta_cutoff(
    logits: torch.Tensor,
    eta_cutoff: torch.Tensor,
) -> torch.Tensor:
    shifted_logits = torch.log_softmax(logits, dim=-1)
    probs = shifted_logits.exp()

    neg_entropy = (probs * shifted_logits).nansum(dim=-1)
    eps = torch.min(eta_cutoff,
                    torch.sqrt(eta_cutoff) *
                    torch.exp(neg_entropy)).unsqueeze(dim=1)

    eta_mask = probs < eps

    # guard against nulling out all the logits
    top_idx = torch.argmax(probs, dim=1, keepdim=True)
    eta_mask.scatter_(dim=1, index=top_idx, value=False)

    logits[eta_mask] = -float("inf")
    return logits


def _apply_epsilon_cutoff(
    logits: torch.Tensor,
    epsilon_cutoff: torch.Tensor,
) -> torch.Tensor:
    probs = logits.softmax(dim=-1)

    eps_mask = probs < epsilon_cutoff.unsqueeze(dim=1)

    # guard against nulling out all the logits
    top_idx = torch.argmax(probs, dim=1, keepdim=True)
    eps_mask.scatter_(dim=1, index=top_idx, value=False)

    logits[eps_mask] = -float("inf")
    return logits


def _apply_typical_sampling(
    logits: torch.Tensor,
    typical_p: torch.Tensor,
) -> torch.Tensor:
    shifted_logits = torch.log_softmax(logits, dim=-1)
    probs = shifted_logits.exp()

    neg_entropy = (probs * shifted_logits).nansum(dim=-1, keepdim=True)

    surprisal_deviations = (neg_entropy - shifted_logits).abs()
    _, indices = torch.sort(surprisal_deviations)
    reordered_probs = probs.gather(-1, indices)
    typ_mask_sorted = reordered_probs.cumsum(dim=-1) >= typical_p.unsqueeze(
        dim=1)

    min_tokens_to_keep = 1
    # Keep at least min_tokens_to_keep
    typ_mask_sorted[..., :min_tokens_to_keep] = 0

    typ_mask = typ_mask_sorted.scatter(1, indices, typ_mask_sorted)
    logits[typ_mask] = -float("inf")
    return logits


def _apply_quadratic_sampling(
    logits: torch.Tensor,
    smoothing_factor: torch.Tensor,
    smoothing_curve: torch.Tensor,
) -> torch.Tensor:
    """
    Applies a quadratic transformation to the logits based on the
    provided smoothing factors and curves. The transformation is
    centered around the maximum logit value in the batch.
    The transformation involves a quadratic and cubic term, with the
    cubic term controlled by the smoothing curve. The quadratic term is
    scaled by the smoothing factor, and the cubic term is scaled by the
    product of the smoothing factor and the smoothing curve.
    params:
        logits (torch.Tensor): The logits to be transformed.
        smoothing_factors (torch.Tensor): The factors to scale the quadratic
            term in the transformation.
        smoothing_curves (torch.Tensor): The factors to scale the cubic term
            in the transformation.
    returns:
        torch.Tensor: The transformed logits.
    Credits: @kalomaze
    """
    max_logits = logits.max(dim=-1, keepdim=True).values
    diff = logits - max_logits
    smoothing_factor.unsqueeze_(dim=1)
    smoothing_curve.unsqueeze_(dim=1)

    k = (3 - smoothing_curve) / 2
    s = (smoothing_curve - 1) / 2

    mask = smoothing_factor > 0
    mask = mask.flatten()
    transformed_logits = torch.where(
        logits != float('-inf'), -(k * smoothing_factor * diff**2) +
        (s * smoothing_factor * diff**3) + max_logits, logits)
    logits[mask, :] = transformed_logits[mask, :]
    return logits


def _greedy_sample(
    selected_seq_groups: List[SequenceGroupToSample],
    samples: torch.Tensor,
) -> List[Tuple[List[int], List[int]]]:
    """Run greedy sampling on a given samples.
    Args:
        selected_seq_groups: A list of sequence groups batched.
        samples: (num_selected_samples,) A tensor of samples. The length of
            samples could be smaller than selected_seq_groups if
            seq_group.do_sample is False.
    Returns:
        Tuple of (next_token_ids, parent_ids). The length of returned list is
        same as the length of selected_seq_groups. If the corresponding
        seq_group has do_sample=False, tuple contains ([], [])
    """
    samples = samples.tolist()
    sample_idx = 0
    results = []
    for seq_group in selected_seq_groups:
        if not seq_group.do_sample:
            results.append(([], []))
            continue

        seq_ids = seq_group.seq_ids
        num_parent_seqs = len(seq_ids)
        assert num_parent_seqs == 1, (
            "Greedy sampling should have only one seq.")
        parent_ids = list(range(num_parent_seqs))
        next_token_ids = [samples[sample_idx]]
        results.append((next_token_ids, parent_ids))
        sample_idx += num_parent_seqs
    return results


def _random_sample(
    selected_seq_groups: List[SequenceGroupToSample],
    random_samples: torch.Tensor,
) -> List[Tuple[List[int], List[int]]]:
    """Run random sampling on a given samples.
    Args:
        selected_seq_groups: A list of sequence groups batched.
        random_samples: (num_selected_samples,) A tensor of samples. The
            length of samples could be smaller than selected_seq_groups if
            seq_group.do_sample is False.
    Returns:
        Tuple of (next_token_ids, parent_ids). The length of returned list is
        same as the length of selected_seq_groups. If the corresponding
        seq_group has do_sample=False, tuple contains ([], [])
    """
    # Find the maximum best_of value of the prompt phase requests.
    random_samples = random_samples.cpu()
    sample_idx = 0
    results = []
    for seq_group in selected_seq_groups:
        if not seq_group.do_sample:
            results.append(([], []))
            continue

        seq_ids = seq_group.seq_ids
        sampling_params = seq_group.sampling_params
        is_prompt = seq_group.is_prompt
        num_parent_seqs = len(seq_ids)
        if is_prompt:
            # Prompt phase.
            parent_ids = [0] * sampling_params.best_of
            next_token_ids = random_samples[
                sample_idx, :sampling_params.best_of].tolist()
        else:
            # Generation phase.
            parent_ids = list(range(num_parent_seqs))
            next_token_ids = random_samples[sample_idx:sample_idx +
                                            num_parent_seqs, 0].tolist()
        results.append((next_token_ids, parent_ids))
        sample_idx += num_parent_seqs
    return results


def _beam_search_sample(
    selected_seq_groups: List[SequenceGroupToSample],
    logprobs: torch.Tensor,
) -> List[Tuple[List[int], List[int]]]:
    """Run beam sampling on a given samples.
    Args:
        selected_seq_groups: A list of sequence groups batched.
        logprobs: (num_selected_samples, vocab_size,) A tensor of logprob
        on selected sample indices.
    Returns:
        Tuple of (next_token_ids, parent_ids). The length of returned list is
        same as the length of selected_seq_groups. If the corresponding
        seq_group has do_sample=False, tuple contains ([], [])
    """
    # We sample 2 * beam_width candidates to make sure that with high
    # probability we can get `beam_width` candidates in addition to
    # the finished sequences for the next iteration. See
    # https://github.com/tensorflow/tensor2tensor/blob/bafdc1b67730430d38d6ab802cbd51f9d053ba2e/tensor2tensor/utils/beam_search.py#L557-L563
    # for details. See also HF reference:
    # https://github.com/huggingface/transformers/blob/a4dd53d88e4852f023332d284ff07a01afcd5681/src/transformers/generation/utils.py#L3063-L3065
    #
    # NOTE: Beam search is not vectorized, so its speed can be slower than
    # other sampling methods.
    sample_idx = 0
    results = []
    for seq_group in selected_seq_groups:
        if not seq_group.do_sample:
            results.append(([], []))
            continue

        is_prompt = seq_group.is_prompt
        seq_ids, sampling_params = seq_group.seq_ids, seq_group.sampling_params
        num_parent_seqs = len(seq_ids)
        beam_width = sampling_params.best_of
        seq_group_logprobs = logprobs[sample_idx:sample_idx + num_parent_seqs]
        if is_prompt:
            # Prompt phase.
            assert num_parent_seqs == 1, (
                "Prompt input should have only one seq.")
            parent_ids = [0] * (2 * beam_width)
            _, next_token_ids = torch.topk(seq_group_logprobs[0],
                                           2 * beam_width)
            next_token_ids = next_token_ids.tolist()
        else:
            # Generation phase.
            cumulative_logprobs = [
                seq_group.seq_data[seq_id].cumulative_logprob
                for seq_id in seq_ids
            ]
            cumulative_logprobs = torch.tensor(
                cumulative_logprobs,
                dtype=torch.float,
                device=seq_group_logprobs.device)
            seq_group_logprobs = (seq_group_logprobs +
                                  cumulative_logprobs.unsqueeze(dim=1))
            _, topk_ids = torch.topk(seq_group_logprobs.flatten(),
                                     2 * beam_width)
            topk_ids = topk_ids.tolist()
            vocab_size = seq_group_logprobs.size(-1)
            parent_ids = [i // vocab_size for i in topk_ids]
            next_token_ids = [i % vocab_size for i in topk_ids]
        results.append((next_token_ids, parent_ids))
        sample_idx += num_parent_seqs
    assert sample_idx == logprobs.size(0)
    return results


# torch.multinomial forces a GPU<->CPU sync.
# Therefore, we use an optimized implementation instead.
# Note that we always sample with replacement.
# probs will be modified in place, but this is fine, as we pass
# in a copy already.
def _multinomial(
    probs: torch.Tensor,
    num_samples: int,
    seq_groups: Optional[List[SequenceGroupToSample]] = None,
) -> torch.Tensor:
    if num_samples > 1:
        # This is equivalent to torch.repeat_interleaved (which also
        # forces a GPU<->CPU sync).
        # This allows us to do sampling with replacement by creating
        # num_samples copies of each row in the tensor, and then
        # batch sampling the resulting tensor.
        probs = probs[:, None, :].expand(probs.shape[0], num_samples,
                                         probs.shape[1]).contiguous().view(
                                             -1, probs.shape[1])
    q = torch.empty_like(probs)
    if seq_groups is None:
        q.exponential_()
    else:
        sample_idx = 0
        for seq_group in seq_groups:
            seq_ids = seq_group.seq_ids
            next_sample_idx = sample_idx + len(seq_ids) * num_samples
            q[sample_idx:next_sample_idx].exponential_(
                generator=seq_group.generator)
            sample_idx = next_sample_idx
    return probs.div_(q).argmax(dim=1).view(-1, num_samples)


def _sample_with_torch(
    probs: torch.Tensor,
    logprobs: torch.Tensor,
    sampling_metadata: SamplingMetadata,
    include_gpu_probs_tensor: bool,
    modify_greedy_probs: bool,
) -> Tuple[List[Tuple[List[int], List[int]]], Optional[torch.Tensor]]:
    categorized_seq_group_ids = {t: [] for t in SamplingType}
    categorized_sample_indices = sampling_metadata.categorized_sample_indices
    for i, seq_group in enumerate(sampling_metadata.seq_groups):
        sampling_params = seq_group.sampling_params
        sampling_type = sampling_params.sampling_type
        categorized_seq_group_ids[sampling_type].append(i)

    sample_results_dict: Dict[int, Tuple[List[int], List[int]]] = {}
    sample_metadata = {}
    multinomial_samples = {}
    # Create output tensor for sampled token ids.
    if include_gpu_probs_tensor:
        sampled_token_ids_tensor = torch.empty(logprobs.shape[0],
                                               1,
                                               dtype=torch.long,
                                               device=logprobs.device)
    else:
        sampled_token_ids_tensor = None
    # Counterintuitively, having two loops here is actually faster.
    # The first loop can run without waiting on GPU<->CPU sync.
    for sampling_type in SamplingType:
        sample_indices = categorized_sample_indices[sampling_type][:, 0]
        num_tokens = len(sample_indices)
        if num_tokens == 0:
            continue

        seq_group_id = categorized_seq_group_ids[sampling_type]
        seq_groups = [sampling_metadata.seq_groups[i] for i in seq_group_id]
        sample_metadata[sampling_type] = (seq_group_id, seq_groups)
        long_sample_indices = sample_indices.long()
        if sampling_type == SamplingType.GREEDY:
            greedy_samples = torch.argmax(logprobs[long_sample_indices],
                                          dim=-1)
            if include_gpu_probs_tensor:
                # Store sampled tokens in output tensor.
                sampled_token_ids_tensor[
                    long_sample_indices] = greedy_samples.unsqueeze(-1)
            if modify_greedy_probs:
                # If required, modify the probabilities such that sampling from
                # the modified distribution would always sample the argmax
                # token id.
                _modify_greedy_probs_inplace(logprobs, probs,
                                             long_sample_indices,
                                             greedy_samples)

        elif sampling_type in (SamplingType.RANDOM, SamplingType.RANDOM_SEED):
            max_best_of_in_batch = 1
            for seq_group in seq_groups:
                if seq_group.is_prompt:
                    sampling_params = seq_group.sampling_params
                    max_best_of_in_batch = max(max_best_of_in_batch,
                                               sampling_params.best_of)
            seeded_args = {} if sampling_type == SamplingType.RANDOM else {
                "seq_groups": seq_groups,
            }

            multinomial_samples[sampling_type] = _multinomial(
                probs[long_sample_indices], max_best_of_in_batch,
                **seeded_args)
            if include_gpu_probs_tensor:
                # Store sampled tokens in output tensor.
                sampled_token_ids_tensor[
                    long_sample_indices] = multinomial_samples[sampling_type]
        elif sampling_type == SamplingType.BEAM:
            beam_search_logprobs = logprobs[sample_indices]
        else:
            raise ValueError(f"Unsupported sampling type: {sampling_type}")

    # GPU<->CPU sync happens in the loop below.
    # This also converts the sample output to Python objects.
    if not sampling_metadata.skip_sampler_cpu_output:
        for sampling_type in SamplingType:
            if sampling_type not in sample_metadata:
                continue
            (seq_group_id, seq_groups) = sample_metadata[sampling_type]
            if sampling_type == SamplingType.GREEDY:
                sample_results = _greedy_sample(seq_groups, greedy_samples)
            elif sampling_type in (SamplingType.RANDOM,
                                   SamplingType.RANDOM_SEED):
                sample_results = _random_sample(
                    seq_groups, multinomial_samples[sampling_type])
            elif sampling_type == SamplingType.BEAM:
                sample_results = _beam_search_sample(seq_groups,
                                                     beam_search_logprobs)
            sample_results_dict.update(zip(seq_group_id, sample_results))

        sample_results = [
            sample_results_dict.get(i, ([], []))
            for i in range(len(sampling_metadata.seq_groups))
        ]
    else:
        sample_results = []

    return sample_results, sampled_token_ids_tensor


def _sample_with_triton_kernel(
    probs: torch.Tensor,
    logprobs: torch.Tensor,
    sampling_metadata: SamplingMetadata,
    sampling_tensors: SamplingTensors,
) -> List[Tuple[List[int], List[int]]]:
    categorized_seq_group_ids = {t: [] for t in SamplingType}
    categorized_sample_indices = sampling_metadata.categorized_sample_indices
    for i, seq_group in enumerate(sampling_metadata.seq_groups):
        sampling_params = seq_group.sampling_params
        sampling_type = sampling_params.sampling_type
        categorized_seq_group_ids[sampling_type].append(i)

    sample_results_dict: Dict[int, Tuple[List[int], List[int]]] = {}
    sample_metadata = {}
    max_best_of_in_batch = 1
    # Counterintuitively, having two loops here is actually faster.
    # The first loop can run without waiting on GPU<->CPU sync.
    for sampling_type in SamplingType:
        sample_indices = categorized_sample_indices[sampling_type][:, 0]
        sampled_token_indices = categorized_sample_indices[sampling_type][:, 1]
        num_tokens = len(sample_indices)
        if num_tokens == 0:
            continue
        seq_group_id = categorized_seq_group_ids[sampling_type]
        seq_groups = [sampling_metadata.seq_groups[i] for i in seq_group_id]
        sample_metadata[sampling_type] = (seq_group_id, seq_groups,
                                          sample_indices,
                                          sampled_token_indices)
        if sampling_type in (SamplingType.GREEDY, SamplingType.RANDOM,
                             SamplingType.RANDOM_SEED):
            for seq_group in seq_groups:
                if seq_group.is_prompt:
                    sampling_params = seq_group.sampling_params
                    max_best_of_in_batch = max(max_best_of_in_batch,
                                               sampling_params.best_of)
        elif sampling_type == SamplingType.BEAM:
            beam_search_logprobs = logprobs[sample_indices]
        else:
            raise ValueError(f"Unsupported sampling type: {sampling_type}")
    sampled_tokens, _, _ = sample_triton(
        probs=probs,
        seeds=sampling_tensors.sampling_seeds,
        max_best_of=max_best_of_in_batch,
        sample_indices=sampling_tensors.sample_indices,
        logprobs=logprobs,
        # don't save logprobs because we have logic for that below
        # TODO: use this instead of the CPU-based logic below
        save_logprobs=False,
    )
    # GPU<->CPU sync happens in the loop below.
    for sampling_type in SamplingType:
        if sampling_type not in sample_metadata:
            continue
        (seq_group_id, seq_groups, sample_indices,
         sampled_token_indices) = sample_metadata[sampling_type]
        if sampling_type == SamplingType.GREEDY:
            sample_results = _greedy_sample(
                seq_groups, sampled_tokens[sampled_token_indices][:, 0])
        elif sampling_type in (SamplingType.RANDOM, SamplingType.RANDOM_SEED):
            sample_results = _random_sample(
                seq_groups, sampled_tokens[sampled_token_indices])
        elif sampling_type == SamplingType.BEAM:
            sample_results = _beam_search_sample(seq_groups,
                                                 beam_search_logprobs)
        sample_results_dict.update(zip(seq_group_id, sample_results))

    sample_results = [
        sample_results_dict.get(i, ([], []))
        for i in range(len(sampling_metadata.seq_groups))
    ]
    return sample_results


def _sample(
    probs: torch.Tensor, logprobs: torch.Tensor,
    sampling_metadata: SamplingMetadata, sampling_tensors: SamplingTensors,
    include_gpu_probs_tensor: bool, modify_greedy_probs: bool
) -> Tuple[List[Tuple[List[int], List[int]]], Optional[torch.Tensor]]:
    """
    Args:
        probs: (num_query_tokens_in_batch, num_vocab)
        logprobs: (num_query_tokens_in_batch, num_vocab)
        sampling_metadata: The metadata for a batch for sampling.
        sampling_tensors: Tensors that include sampling related metadata.
    Returns:
        (next_token_ids, parent_seq_ids) for each seq group in a batch.
            If sampling is skipped, it returns ([], [])
        sampled_token_ids_tensor: A tensor of sampled token ids.    
    """
    return _sample_with_torch(
        probs,
        logprobs,
        sampling_metadata,
        include_gpu_probs_tensor=include_gpu_probs_tensor,
        modify_greedy_probs=modify_greedy_probs,
    )
    # TODO: Enable once Triton kernel & associated code is faster.
    # return _sample_with_triton_kernel(probs, logprobs, sampling_metadata,
    #                                   sampling_tensors)


def _get_ranks(x: torch.Tensor, indices: torch.Tensor) -> torch.Tensor:
    """
    This function calculates the ranks of the chosen tokens in a logprob tensor.
    Args:
        x (torch.Tensor): 2D logprob tensor of shape (N, M)
                        where N is the no. of tokens and M is the vocab dim.
        indices (torch.Tensor): List of chosen token indices.
    Returns:
        torch.Tensor: 1D tensor of shape (N,) where N is the no. of tokens.
                    Each element in the returned tensor represents the rank 
                    of the chosen token in the input logprob tensor.
    """
    vals = x[torch.arange(0, len(x), device=x.device, dtype=indices.dtype),
             indices]
    return (x > vals[:, None]).long().sum(1).add_(1)


def _get_logprobs(
    logprobs: torch.Tensor,
    sampling_metadata: SamplingMetadata,
    sample_results: List[Tuple[List[int], List[int]]],
) -> Tuple[List[Optional[PromptLogprobs]], List[SampleLogprobs]]:
    """Return sample lobprobs and prompt logprobs.
    The logic consists of 3 parts.
    - Select indices to compute logprob from, ranks of token ids, and
        the top k token ids from logprobs.
    - Compute prompt logprobs if required.
    - Compute sample logprobs if required.
    Args:
        logprobs: (num_query_tokens_across_batch, num_vocab). Each query token's
            logprob per vocab. Sequence groups' query tokens are batched in a
            single flattened tensor. For example, assuming there are N
            seq groups, it is sorted by prefill tokens for seq_group_1 (if
            prompt logprob is enabled), decode tokens for seq_group_1 (if
            sampling is required), prefill tokens for seq_group_2, ...
        sampling_metadata: The sampling metadata.
        sample_results: (num_seq_groups) The tuple of (next_token_ids,
            parent_ids) for each sequence group. When beam search is enabled,
            sample_results can contain different number of seq_ids from
            sampling_metadata.seq_groups. It is because beam search creates
            2 * BEAM_WIDTH number of samples (whereas there are only up to
            BEAM_WIDTH number of seq_ids).
    Returns:
        A tuple of prompt and sample logprobs per sequence group in a batch.
    """
    # The index of query token to calculate logprobs. It includes both
    # prompt and sample logprob indices.
    query_indices: List[int] = []
    # The next token ids to get the logprob value from.
    next_token_ids: List[int] = []
    # The largest requested number of logprobs. We find logprobs as many as the
    # largest num logprobs in this API. If every logprobs is None, it will be
    # set to -1.
    largest_num_logprobs = -1
    # If beam search is enabled.
    use_beam_search = False

    # Select indices to compute logprob from, ranks of token ids, and the top
    # k token ids from logprobs.
    for (seq_group, sample_result) in zip(sampling_metadata.seq_groups,
                                          sample_results):
        sampling_params = seq_group.sampling_params

        # Update indices and tokens for prompt logprobs.
        if (seq_group.is_prompt
                and sampling_params.prompt_logprobs is not None):
            largest_num_logprobs = max(largest_num_logprobs,
                                       sampling_params.prompt_logprobs)
            next_prompt_tokens = _get_next_prompt_tokens(seq_group)
            query_indices.extend(seq_group.prompt_logprob_indices)
            next_token_ids.extend(next_prompt_tokens)

        # Update indices and next tokenes for sample logprob.
        if seq_group.do_sample:
            token_ids, parent_seq_ids = sample_result
            # NOTE: We cannot directly use sample_indices because
            # sample_indices only contain parent seq_ids of a previous step.
            # The current step may have different number of seq_ids, and
            # we can obtain it from `sample_result[1]`.
            query_idx = seq_group.sample_indices[0]
            query_indices.extend(
                [query_idx + parent_id for parent_id in parent_seq_ids])
            next_token_ids.extend(token_ids)

            if sampling_params.logprobs is not None:
                largest_num_logprobs = max(largest_num_logprobs,
                                           sampling_params.logprobs)

            use_beam_search = use_beam_search or sampling_params.use_beam_search

        assert len(next_token_ids) == len(query_indices)

    if len(query_indices) == 0:
        empty_sampled_logprob = []
        empty_prompt_logprob = None
        return [empty_prompt_logprob], [empty_sampled_logprob]

    selected_logprobs, ranks = None, None
    top_logprobs, top_token_ids = None, None

    # If largest_num_logprobs == -1, i.e. no logprobs are requested, we can
    # skip the whole logprob calculation.
    if largest_num_logprobs >= 0 or use_beam_search:
        query_indices_gpu = torch.tensor(query_indices, device=logprobs.device)
        next_token_ids_gpu = torch.tensor(next_token_ids,
                                          device=logprobs.device)

        # (num_selected_query_tokens, num_logprobs). Note that query_indices can
        # contain duplicates if beam search is enabled.
        selected_logprobs = logprobs[[
            query_indices_gpu,
            next_token_ids_gpu,
        ]]
        ranks = _get_ranks(
            logprobs[query_indices_gpu],
            next_token_ids_gpu,
        )
        assert selected_logprobs.shape[0] == ranks.shape[0]

        # We need to compute top k only if there exists logprobs > 0.
        if largest_num_logprobs > 0:
            # Logprobs of topk tokens for a batch of sequence groups.
            # (num_query_tokens_across_batch).
            top_logprobs, top_token_ids = torch.topk(logprobs,
                                                     largest_num_logprobs,
                                                     dim=-1)
            top_logprobs = top_logprobs.to('cpu')
            top_token_ids = top_token_ids.to('cpu')

        selected_logprobs = selected_logprobs.to('cpu')
        ranks = ranks.to('cpu')

    # Find prompt/sample logprobs.
    prompt_logprobs_per_seq_group: List[Optional[PromptLogprobs]] = []
    sample_logprobs_per_seq_group: List[SampleLogprobs] = []
    top_logprob_idx = 0
    selected_logprobs_idx = 0

    for seq_group, sample_result in zip(sampling_metadata.seq_groups,
                                        sample_results):
        (prompt_logprobs, top_logprob_idx,
         selected_logprobs_idx) = _get_prompt_logprob_if_needed(
             seq_group, selected_logprobs, ranks, top_token_ids, top_logprobs,
             selected_logprobs_idx, top_logprob_idx)
        prompt_logprobs_per_seq_group.append(prompt_logprobs)

        (sampled_logprobs, top_logprob_idx,
         selected_logprobs_idx) = _get_sampled_logprob_if_needed(
             seq_group, sample_result, selected_logprobs, ranks, top_token_ids,
             top_logprobs, selected_logprobs_idx, top_logprob_idx)
        sample_logprobs_per_seq_group.append(sampled_logprobs)

    return prompt_logprobs_per_seq_group, sample_logprobs_per_seq_group


def _get_prompt_logprob_if_needed(
    seq_group: SequenceGroupToSample,
    selected_logprobs: torch.Tensor,
    ranks: torch.Tensor,
    top_token_ids: torch.Tensor,
    top_logprobs: torch.Tensor,
    selected_logprobs_idx: int,
    top_logprob_idx: int,
):
    """Compute the prompt logprob from a sequence group if needed."""
    sampling_params = seq_group.sampling_params
    is_prompt = seq_group.is_prompt

    # Find prompt logprobs
    prompt_logprobs: Optional[PromptLogprobs] = None
    if is_prompt and sampling_params.prompt_logprobs is not None:
        prompt_logprobs = []
        num_logprobs = sampling_params.prompt_logprobs
        next_prompt_tokens = _get_next_prompt_tokens(seq_group)
        # Pre-select indexes and create a list. It is faster than calling .item
        # repetitively.
        selected_logprob_items = selected_logprobs[
            selected_logprobs_idx:selected_logprobs_idx +
            len(next_prompt_tokens)].tolist()
        rank_items = ranks[selected_logprobs_idx:selected_logprobs_idx +
                           len(next_prompt_tokens)].tolist()

        for idx, token_id in enumerate(next_prompt_tokens):
            # Calculate the prompt logprob of the real prompt tokens.
            # {token_id: (logprob, rank_from_vocab)}
            prompt_logprobs_dict: Dict[int, Tuple[float, int]] = {
                token_id: (selected_logprob_items[idx], rank_items[idx])
            }

            # Add top K prompt logprobs along with its rank.
            if num_logprobs > 0:
                top_ids = top_token_ids[
                    top_logprob_idx, :num_logprobs].tolist()
                top_probs = top_logprobs[
                    top_logprob_idx, :num_logprobs].tolist()
                # Top K is already sorted by rank, so we can use 1 ~
                # num_logprobs + 1 for rank.
                top_ranks = range(1, num_logprobs + 1)
                prompt_logprobs_dict.update({
                    top_id: (top_prob, rank)
                    for top_id, top_prob, rank in zip(top_ids, top_probs,
                                                      top_ranks)
                })
            prompt_logprobs.append({
                token_id: Logprob(*logprob_and_rank)
                for token_id, logprob_and_rank in prompt_logprobs_dict.items()
            })
            # + 1 to go to the next prompt token.
            top_logprob_idx += 1

        # + len(next_prompt_tokens) to go to the next prompt.
        selected_logprobs_idx += len(next_prompt_tokens)
    return prompt_logprobs, top_logprob_idx, selected_logprobs_idx


def _get_sampled_logprob_if_needed(
    seq_group: SequenceGroupToSample,
    sample_result: Tuple[List[int], List[int]],
    selected_logprobs: torch.Tensor,
    ranks: torch.Tensor,
    top_token_ids: torch.Tensor,
    top_logprobs: torch.Tensor,
    selected_logprobs_idx: int,
    top_logprob_idx: int,
):
    """Compute the sample logprob if needed."""
    seq_ids = seq_group.seq_ids
    num_logprobs = seq_group.sampling_params.logprobs
    use_beam_search = seq_group.sampling_params.use_beam_search
    sampled_logprobs: SampleLogprobs = []
    next_token_ids, parent_seq_ids = sample_result

    if seq_group.do_sample:
        assert len(next_token_ids) > 0
        if num_logprobs is None and not use_beam_search:
            for next_token_id in next_token_ids:
                # Use a dummy logprob
                sampled_logprobs.append({next_token_id: Logprob(inf)})
        else:
            # Pre-select items from tensor. tolist() is faster than repetitive
            # `.item()` calls.
            selected_logprob_items = selected_logprobs[
                selected_logprobs_idx:selected_logprobs_idx +
                len(next_token_ids)].tolist()
            rank_items = ranks[selected_logprobs_idx:selected_logprobs_idx +
                               len(next_token_ids)].tolist()
            for idx, (next_token_id, parent_id) in enumerate(
                    zip(next_token_ids, parent_seq_ids)):
                # Get the logprob of a sampled token.
                sampled_logprobs_dict = {
                    next_token_id:
                    (selected_logprob_items[idx], rank_items[idx])
                }
                if num_logprobs is not None and num_logprobs > 0:
                    # Get top K logprobs.
                    top_ids = top_token_ids[top_logprob_idx +
                                            parent_id, :num_logprobs].tolist()
                    top_probs = top_logprobs[
                        top_logprob_idx + parent_id, :num_logprobs].tolist()
                    # Top K is already sorted by rank, so we can use 1 ~
                    # num_logprobs + 1 for rank.
                    top_ranks = range(1, num_logprobs + 1)
                    sampled_logprobs_dict.update({
                        top_id: (top_prob, rank)
                        for top_id, top_prob, rank in zip(
                            top_ids, top_probs, top_ranks)
                    })

                sampled_logprobs.append({
                    token_id: Logprob(*logprob_and_rank)
                    for token_id, logprob_and_rank in
                    sampled_logprobs_dict.items()
                })

        # NOTE: This part of code is not intuitive. `selected_logprobs` include
        # logprobs for the current step, which has len(next_token_ids) tokens
        # per sequence group. `logprobs` includes logprobs from the previous
        # steps, which has len(seq_ids) tokens per sequence group.
        # Iterate to the next sequence group in a batch.
        selected_logprobs_idx += len(next_token_ids)
        # Iterate to the next sequence group in a batch.
        top_logprob_idx += len(seq_ids)
    return sampled_logprobs, top_logprob_idx, selected_logprobs_idx


def _modify_greedy_probs_inplace(logprobs: torch.Tensor, probs: torch.Tensor,
                                 sample_indices: torch.Tensor,
                                 greedy_samples: torch.Tensor) -> None:
    """Modify the probability distributions of the greedily-sampled tokens such
    that each sampled token has a "probability" of 1.0. This is required by
    speculative decoding, which depends on the sampling method being encoded
    within the probability distribution for correctness.
    # Why do we only need to do this for greedy sampling?
    Aphrodite's sampler performs the following steps for greedy or multinomial
    (random) sampling:
        1. Get logits from model.
        2. Modify logits according to per-sequence sampling parameters.
            - Multiply by temperature, top-k and top-p masking, penalize tokens
                according to their frequency, etc.
        3. Sample a token.
            - Random sampling simply samples from the modified probability
                distribution.
            - Greedy sampling performs `argmax` to obtain the token with the
                highest likelihood.
    
    Ignoring greedy sampling for a moment, we find that the computed probability
    distribution has the following property: we can sample from it independently
    and find that the token sampled by the Sampler has a frequency corresponding
    to how often we see it in our sampling. In other words, for tokens sampled
    with Aphrodite's random SamplingType, the computed probability distribution
    encodes the sampling methodology completely.
    Greedy sampling does not normally have this property. Aphrodite modifies
    logits according to sampling params, then performs `argmax`, then returns
    the sampled token and the computed probability distribution. If we sample
    from the distribution, we'll find the likelihood of the greedily-sampled
    token is not always 1.0.
    Since lossless speculative decoding requires that the sampling methodology
    be encoded within the probability distribution, we are motivated to modify
    the probability distribution such that the sampled token has probability 1
    when speculative decoding is used.
    NOTE: Alternatively, we could use an extremely low temperature to achieve
    greedy sampling using multinomial computation and unite the codepaths. This
    has implications on the overall design of the sampler, e.g. how to record
    accurate logprobs for the user, so this improvement is deferred to later.
    """
    # NOTE: logprobs are not modified so they can be returned to the user.
    probs[sample_indices, :] = 0
    probs[sample_indices, greedy_samples] = 1.0


def _build_sampler_output(
    sample_results: SampleResultType,
    sampling_metadata: SamplingMetadata,
    prompt_logprobs: Optional[List[Optional[PromptLogprobs]]],
    sample_logprobs: Optional[List[SampleLogprobs]],
    on_device_tensors: Optional[Tuple[torch.Tensor, torch.Tensor,
                                      torch.Tensor]],
    skip_sampler_cpu_output: bool = False,
) -> SamplerOutput:
    """Construct Python objects with the output of sampling.
    Args:
        on_device_tensors: Tuple containing on-device tensors with the
            probabilities used in sampling and the sampled token ids. This
            allows post-processing without copies to CPU/serialization, e.g. in
            speculative decoding rejection sampling.
    """
    sampler_output: List[CompletionSequenceGroupOutput] = []
    if not skip_sampler_cpu_output:
        assert prompt_logprobs is not None
        assert sample_logprobs is not None

        for (seq_group, sample_result, group_prompt_logprobs,
             group_sample_logprobs) in zip(sampling_metadata.seq_groups,
                                           sample_results, prompt_logprobs,
                                           sample_logprobs):
            seq_ids = seq_group.seq_ids
            next_token_ids, parent_ids = sample_result
            seq_outputs: List[SequenceOutput] = []
            for parent_id, next_token_id, logprobs in zip(
                    parent_ids, next_token_ids, group_sample_logprobs):
                seq_outputs.append(
                    SequenceOutput(seq_ids[parent_id], next_token_id,
                                   logprobs))
            sampler_output.append(
                CompletionSequenceGroupOutput(seq_outputs,
                                              group_prompt_logprobs))
    # If not specified, store None values in SamplerOutput.
    if on_device_tensors is not None:
        (sampled_token_probs, logprobs_tensor,
         sampled_token_ids) = on_device_tensors
    else:
        sampled_token_probs, logprobs_tensor, sampled_token_ids = (None, None,
                                                                   None)
    return SamplerOutput(
        outputs=sampler_output,
        sampled_token_probs=sampled_token_probs,
        sampled_token_ids=sampled_token_ids,
        logprobs=logprobs_tensor,
    )


def _get_next_prompt_tokens(seq_group: SequenceGroupToSample) -> List[str]:
    """Get a list of next prompt tokens to compute logprob from a
        given sequence group.
    It is used to compute prompt logprob. Imagine you have logprob for each
    query token. Query token needs to know the next prompt token id to compute
    prompt logprob. This is a helper to obtain next prompt token ids.
    This API has to be used only when the caller knows seq_group is in prefill
    stage.
    Returns:
        A list of next prompt tokens to compute logprob.
    """
    assert seq_group.is_prompt, (
        "Caller should ensure the sequence group is in a prefill stage.")
    seq_ids = seq_group.seq_ids
    query_len = seq_group.query_len
    assert query_len is not None
    # prompt has only 1 seq id.
    assert len(seq_ids) == 1
    seq_data = seq_group.seq_data[seq_ids[0]]
    computed_len = seq_data.get_num_computed_tokens()
    prompt_tokens = seq_data.prompt_token_ids
    # +1 because we are looking for a next prompt token.
    next_token_index_start = computed_len + 1
    next_token_index_end = min(computed_len + query_len + 1,
                               len(prompt_tokens))
    next_prompt_tokens = prompt_tokens[
        next_token_index_start:next_token_index_end]
    return next_prompt_tokens


# def _apply_mirostat_v2(logits: torch.Tensor,
#                        sampling_tensors: SamplingTensors) -> torch.Tensor:
#     # Reduce our view to just the affected logits
#     logit_view = logits[sampling_tensors.miro_indices]

#     # Calculate surprise value per token
#     #  Convert nats to bits for compatibility with ooba/kobold parameters.
#     logit_surprise = torch.log_softmax(logit_view, dim=-1) / -math.log(2)

#     # Mask out "too-surprising" tokens (surprisal > mu)
#     mus = sampling_tensors.miro_mus
#     miro_mask = logit_surprise > mus.unsqueeze(dim=-1)

#     # Unmask most-likely logit to guarantee a selection.
#     maxinds = torch.argmax(logit_view, dim=-1, keepdim=True)
#     miro_mask.scatter_(dim=1, index=maxinds, value=False)

#     # Apply logit mask (effectively a top-k filter).
#     logit_view[miro_mask] = -float("inf")

#     # Project logit changes made to the view onto the original.
#     # I think this step might be redundant.
#     logits[sampling_tensors.miro_indices] = logit_view
#     return logits

# def _mirostat_store_args(logits: torch.Tensor, args: SamplingTensors,
#                          sample_results: List[Tuple[List[int], List[int]]],
#                          sampling_metadata: SamplingMetadata,
#                          output_metadata: OutputMetadata) -> None:
#     """Based on whichever token was finally sampled, we calculate the
#     final surprisal values to update the mus.

#     Because a single sequence can have multiple samples, we must fork
#     the mu accordingly."""
#     assert sampling_metadata.seq_groups is not None
#     seqid_to_tokens = {}
#     seqid_to_indices = {}
#     for (sids, _), (toks, parents) in zip(sampling_metadata.seq_groups,
#                                           sample_results):
#         for idx, (token, parent) in enumerate(zip(toks, parents)):
#             seqid_to_tokens.setdefault(sids[parent], []).append(token)
#             seqid_to_indices.setdefault(sids[parent], []).append(idx)

#     seqids = args.miro_seqids

#     picked_tokens = torch.tensor([seqid_to_tokens[x] for x in seqids],
#                                  device=logits.device,
#                                  dtype=torch.long)

#     # Clumsily, we recalculate token surprisals.
#     logits_view = logits[args.miro_indices]
#     picked_surprise = torch.gather(torch.log_softmax(logits_view, dim=-1),
#                                    dim=-1,
#                                    index=picked_tokens) / -math.log(2)

#     taus = args.miro_taus.unsqueeze(dim=-1)  # AKA target surprisals
#     etas = args.miro_etas.unsqueeze(dim=-1)  # AKA accumulation rates
#     mus = args.miro_mus.unsqueeze(dim=-1)  # AKA surprisal accumulators
#     nu_mus = mus - (picked_surprise - taus) * etas

#     # Record updated mu values for use in the next iteration
#     # Note how each mu is split into multiple based on the number of samples.
#     for seqid, seq_mus in zip(seqids, nu_mus):
#         for sample_idx, mu in zip(seqid_to_indices[seqid], seq_mus):
#             output_metadata.add(seqid, sample_idx, "miro_mu", mu)
