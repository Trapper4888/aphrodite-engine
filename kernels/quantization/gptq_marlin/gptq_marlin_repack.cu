#include "gptq_marlin.cuh"

namespace gptq_marlin {

static constexpr int repack_stages = 8;

static constexpr int repack_threads = 256;

static constexpr int tile_k_size = tile_size;
static constexpr int tile_n_size = tile_k_size * 4;

#if defined(__CUDA_ARCH__) && __CUDA_ARCH__ < 800

template <int const num_threads, bool const has_perm>
__global__ void
marlin_repack_kernel(uint32_t const *__restrict__ b_q_weight_ptr,
                     uint32_t const *__restrict__ perm_ptr,
                     uint32_t *__restrict__ out_ptr, int size_k, int size_n) {}

} // namespace gptq_marlin

torch::Tensor gptq_marlin_repack(torch::Tensor &b_q_weight, torch::Tensor &perm,
                                 int64_t size_k, int64_t size_n) {
  TORCH_CHECK_NOT_IMPLEMENTED(
      false, "marlin_repack_from_gptq(..) requires CUDA_ARCH >= 8.0");
  return torch::empty({1, 1});
}

#else

template <int const num_threads, bool const has_perm>
__global__ void
marlin_repack_kernel(uint32_t const *__restrict__ b_q_weight_ptr,
                     uint32_t const *__restrict__ perm_ptr,
                     uint32_t *__restrict__ out_ptr, int size_k, int size_n) {
  int k_tiles = size_k / tile_k_size;
  int n_tiles = size_n / tile_n_size;
  int block_k_tiles = div_ceil(k_tiles, gridDim.x);

  int start_k_tile = blockIdx.x * block_k_tiles;
  if (start_k_tile >= k_tiles) {
    return;
  }

  int finish_k_tile = min(start_k_tile + block_k_tiles, k_tiles);

  // Wait until the next thread tile has been loaded to shared memory.
  auto wait_for_stage = [&]() {
    // We only have `stages - 2` active fetches since we are double buffering
    // and can only issue the next fetch when it is guaranteed that the previous
    // shared memory load is fully complete (as it may otherwise be
    // overwritten).
    cp_async_wait<repack_stages - 2>();
    __syncthreads();
  };

  extern __shared__ int4 sh[];

  constexpr int perm_size = tile_k_size / 4;

  int4 *sh_perm_ptr = sh;
  int4 *sh_pipe_ptr = sh_perm_ptr;
  if constexpr (has_perm) {
    sh_pipe_ptr += perm_size;
  }

  constexpr int stage_n_threads = tile_n_size / 4;
  constexpr int stage_k_threads =
      has_perm ? tile_k_size : tile_k_size / pack_factor_4bit;
  constexpr int stage_size = stage_k_threads * stage_n_threads;

  auto load_perm_to_shared = [&](int k_tile_id) {
    int first_k_int4 = (k_tile_id * tile_k_size) / 4;

    int4 const *perm_int4_ptr = reinterpret_cast<int4 const *>(perm_ptr);

    if (threadIdx.x < perm_size) {
      sh_perm_ptr[threadIdx.x] = perm_int4_ptr[first_k_int4 + threadIdx.x];
    }
    __syncthreads();
  };

  auto fetch_to_shared = [&](int pipe, int k_tile_id, int n_tile_id) {
    if (n_tile_id >= n_tiles) {
      cp_async_fence();
      return;
    }

    int first_n = n_tile_id * tile_n_size;

    int4 *sh_ptr = sh_pipe_ptr + stage_size * pipe;

    if constexpr (has_perm) {
      if (threadIdx.x < stage_size) {
        int k_id = threadIdx.x / stage_n_threads;
        int n_id = threadIdx.x % stage_n_threads;

        uint32_t const *sh_perm_int_ptr =
            reinterpret_cast<uint32_t const *>(sh_perm_ptr);

        int src_k = sh_perm_int_ptr[k_id];
        int src_k_packed = src_k / pack_factor_4bit;

        cp_async4_stream(
            &sh_ptr[k_id * stage_n_threads + n_id],
            reinterpret_cast<int4 const *>(&(
                b_q_weight_ptr[src_k_packed * size_n + first_n + (n_id * 4)])));
      }

    } else {
      if (threadIdx.x < stage_size) {
        int k_id = threadIdx.x / stage_n_threads;
        int n_id = threadIdx.x % stage_n_threads;

        int first_k = k_tile_id * tile_k_size;
        int first_k_packed = first_k / pack_factor_4bit;

        cp_async4_stream(&sh_ptr[k_id * stage_n_threads + n_id],
                         reinterpret_cast<int4 const *>(
                             &(b_q_weight_ptr[(first_k_packed + k_id) * size_n +
                                              first_n + (n_id * 4)])));
      }
    }

    cp_async_fence();
  };

  auto repack_tile = [&](int pipe, int k_tile_id, int n_tile_id) {
    if (n_tile_id >= n_tiles) {
      return;
    }

    int warp_id = threadIdx.x / 32;
    int th_id = threadIdx.x % 32;

    if (warp_id >= 4) {
      return;
    }

    int tc_col = th_id / 4;
    int tc_row = (th_id % 4) * 2;

    constexpr int tc_offsets[4] = {0, 1, 8, 9};

    int cur_n = warp_id * 16 + tc_col;

    constexpr int sh_stride = 64;

    int4 *sh_stage_ptr = sh_pipe_ptr + stage_size * pipe;
    uint32_t *sh_stage_int_ptr = reinterpret_cast<uint32_t *>(sh_stage_ptr);

    uint32_t *sh_perm_int_ptr = reinterpret_cast<uint32_t *>(sh_perm_ptr);

    uint32_t vals[pack_factor_4bit];

    if constexpr (has_perm) {
      for (int i = 0; i < 4; i++) {
        int k_idx = tc_row + tc_offsets[i];

        uint32_t src_k = sh_perm_int_ptr[k_idx];
        uint32_t src_k_pos = src_k % pack_factor_4bit;

        uint32_t b1_val = sh_stage_int_ptr[k_idx * sh_stride + cur_n];
        uint32_t b1_cur_val = (b1_val >> (src_k_pos * 4)) & 0xf;

        uint32_t b2_val = sh_stage_int_ptr[k_idx * sh_stride + cur_n + 8];
        uint32_t b2_cur_val = (b2_val >> (src_k_pos * 4)) & 0xf;

        vals[i] = b1_cur_val;
        vals[4 + i] = b2_cur_val;
      }

    } else {

      uint32_t b1_val_1 = sh_stage_int_ptr[cur_n];
      uint32_t b1_val_2 = sh_stage_int_ptr[sh_stride + cur_n];

      uint32_t b2_val_1 = sh_stage_int_ptr[cur_n + 8];
      uint32_t b2_val_2 = sh_stage_int_ptr[sh_stride + cur_n + 8];

#pragma unroll
      for (int i = 0; i < 2; i++) {
        int cur_elem = tc_row + tc_offsets[i];
        vals[i] = (b1_val_1 >> (cur_elem * 4)) & 0xf;
        vals[4 + i] = (b2_val_1 >> (cur_elem * 4)) & 0xf;
      }

#pragma unroll
      for (int i = 2; i < 4; i++) {
        int cur_elem = tc_row + tc_offsets[i] - 8;
        vals[i] = (b1_val_2 >> (cur_elem * 4)) & 0xf;
        vals[4 + i] = (b2_val_2 >> (cur_elem * 4)) & 0xf;
      }
    }

    // Result of:
    // https://github.com/NVIDIA/FasterTransformer/blob/main/src/fastertransformer/cutlass_extensions/include/cutlass_extensions/interleaved_numeric_conversion.h
    constexpr int pack_idx[pack_factor_4bit] = {0, 2, 4, 6, 1, 3, 5, 7};

    uint32_t res = 0;
#pragma unroll
    for (int i = 0; i < pack_factor_4bit; i++) {
      res |= vals[pack_idx[i]] << (i * 4);
    }

    constexpr int tile_size = tile_k_size * tile_n_size / pack_factor_4bit;
    int out_offset = (k_tile_id * n_tiles + n_tile_id) * tile_size;

    out_ptr[out_offset + th_id * 4 + warp_id] = res;
  };

  auto start_pipes = [&](int k_tile_id, int n_tile_id) {
#pragma unroll
    for (int pipe = 0; pipe < repack_stages - 1; pipe++) {
      fetch_to_shared(pipe, k_tile_id, n_tile_id + pipe);
    }

    wait_for_stage();
  };
#pragma unroll
  for (int k_tile_id = start_k_tile; k_tile_id < finish_k_tile; k_tile_id++) {
    int n_tile_id = 0;

    if constexpr (has_perm) {
      load_perm_to_shared(k_tile_id);
    }

    start_pipes(k_tile_id, n_tile_id);

    while (n_tile_id < n_tiles) {
#pragma unroll
      for (int pipe = 0; pipe < repack_stages; pipe++) {
        fetch_to_shared((pipe + repack_stages - 1) % repack_stages, k_tile_id,
                        n_tile_id + pipe + repack_stages - 1);
        repack_tile(pipe, k_tile_id, n_tile_id + pipe);
        wait_for_stage();
      }
      n_tile_id += repack_stages;
    }
  }
}

} // namespace gptq_marlin

torch::Tensor gptq_marlin_repack(torch::Tensor &b_q_weight, torch::Tensor &perm,
                                 int64_t size_k, int64_t size_n) {
  // Verify compatibility with marlin tile of 16x64
  TORCH_CHECK(size_k % gptq_marlin::tile_k_size == 0, "size_k = ", size_k,
              " is not divisible by tile_k_size = ", gptq_marlin::tile_k_size);
  TORCH_CHECK(size_n % gptq_marlin::tile_n_size == 0, "size_n = ", size_n,
              " is not divisible by tile_n_size = ", gptq_marlin::tile_n_size);

  // Verify B
  TORCH_CHECK((size_k / gptq_marlin::pack_factor_4bit) == b_q_weight.size(0),
              "Shape mismatch: b_q_weight.size(0) = ", b_q_weight.size(0),
              ", size_k = ", size_k,
              ", pack_factor_4bit = ", gptq_marlin::pack_factor_4bit);
  TORCH_CHECK(b_q_weight.size(1) == size_n,
              "b_q_weight.size(1) = ", b_q_weight.size(1),
              " is not size_n = ", size_n);

  // Verify device and strides
  TORCH_CHECK(b_q_weight.device().is_cuda(), "b_q_weight is not on GPU");
  TORCH_CHECK(b_q_weight.is_contiguous(), "b_q_weight is not contiguous");
  TORCH_CHECK(b_q_weight.dtype() == at::kInt, "b_q_weight type is not kInt");

  TORCH_CHECK(perm.device().is_cuda(), "perm is not on GPU");
  TORCH_CHECK(perm.is_contiguous(), "perm is not contiguous");
  TORCH_CHECK(perm.dtype() == at::kInt, "perm type is not at::kInt");

  // Alloc buffers
  const at::cuda::OptionalCUDAGuard device_guard(device_of(b_q_weight));
  auto options = torch::TensorOptions()
                     .dtype(b_q_weight.dtype())
                     .device(b_q_weight.device());
  torch::Tensor out = torch::empty(
      {size_k / gptq_marlin::tile_size,
       size_n * gptq_marlin::tile_size / gptq_marlin::pack_factor_4bit},
      options);

  // Detect if there is act_order
  bool has_perm = perm.size(0) != 0;

  // Get ptrs
  uint32_t const *b_q_weight_ptr =
      reinterpret_cast<uint32_t const *>(b_q_weight.data_ptr());
  uint32_t const *perm_ptr =
      reinterpret_cast<uint32_t const *>(perm.data_ptr());
  uint32_t *out_ptr = reinterpret_cast<uint32_t *>(out.data_ptr());

  // Get dev info
  int dev = b_q_weight.get_device();
  cudaStream_t stream = at::cuda::getCurrentCUDAStream(dev);
  int blocks;
  cudaDeviceGetAttribute(&blocks, cudaDevAttrMultiProcessorCount, dev);

  int max_shared_mem = 0;
  cudaDeviceGetAttribute(&max_shared_mem,
                         cudaDevAttrMaxSharedMemoryPerBlockOptin, dev);
  TORCH_CHECK(max_shared_mem > 0);

  if (has_perm) {
    cudaFuncSetAttribute(
        gptq_marlin::marlin_repack_kernel<gptq_marlin::repack_threads, true>,
        cudaFuncAttributeMaxDynamicSharedMemorySize,
        max_shared_mem);
    gptq_marlin::marlin_repack_kernel<gptq_marlin::repack_threads, true>
        <<<blocks, gptq_marlin::repack_threads, max_shared_mem,
           stream>>>(b_q_weight_ptr, perm_ptr, out_ptr, size_k, size_n);

  } else {
    cudaFuncSetAttribute(
        gptq_marlin::marlin_repack_kernel<gptq_marlin::repack_threads, false>,
        cudaFuncAttributeMaxDynamicSharedMemorySize,
        max_shared_mem);
    gptq_marlin::marlin_repack_kernel<gptq_marlin::repack_threads, false>
        <<<blocks, gptq_marlin::repack_threads, max_shared_mem,
           stream>>>(b_q_weight_ptr, perm_ptr, out_ptr, size_k, size_n);
  }

  return out;
}

#endif