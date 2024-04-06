import contextlib
import io
import os
import re
import subprocess
import sys
from typing import List, Set
import warnings
from pathlib import Path

from packaging.version import parse, Version
import setuptools
import torch
import torch.utils.cpp_extension as torch_cpp_ext
from torch.utils.cpp_extension import (BuildExtension, CUDAExtension,
                                       CUDA_HOME, ROCM_HOME)

ROOT_DIR = os.path.dirname(__file__)

MAIN_CUDA_VERSION = "12.1"

# Supported NVIDIA GPU architectures.
NVIDIA_SUPPORTED_ARCHS = {"6.1", "7.0", "7.5", "8.0", "8.6", "8.9", "9.0"}
ROCM_SUPPORTED_ARCHS = {"gfx908", "gfx90a", "gfx942", "gfx1100"}

assert sys.platform.startswith(
    "linux"), "Aphrodite only supports Linux at the moment (including WSL)."

def _is_cuda() -> bool:
    return torch.version.cuda is not None and not _is_neuron()

def _is_hip() -> bool:
    return torch.version.hip is not None


def _is_neuron() -> bool:
    torch_neuronx_installed = True
    try:
        subprocess.run(["neuron-ls"], capture_output=True, check=True)
    except (FileNotFoundError, PermissionError, subprocess.CalledProcessError):
        torch_neuronx_installed = False
    return torch_neuronx_installed



# Compiler flags.
CXX_FLAGS = ["-g", "-O2", "-std=c++17"]
# TODO: Should we use -O3?
NVCC_FLAGS = ["-O2", "-std=c++17"]

if _is_hip():
    if ROCM_HOME is None:
        raise RuntimeError(
            "Cannot find ROCM_HOME. ROCm must be available to build the "
            "package.")
    NVCC_FLAGS += ["-DUSE_ROCM"]

if _is_cuda() and CUDA_HOME is None:
    raise RuntimeError(
        "Cannot find CUDA_HOME. CUDA must be available to build the package.")

ABI = 1 if torch._C._GLIBCXX_USE_CXX11_ABI else 0
CXX_FLAGS += [f"-D_GLIBCXX_USE_CXX11_ABI={ABI}"]
NVCC_FLAGS += [f"-D_GLIBCXX_USE_CXX11_ABI={ABI}"]


def get_hipcc_rocm_version():
    # Run the hipcc --version command
    result = subprocess.run(['hipcc', '--version'],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True)

    # Check if the command was executed successfully
    if result.returncode != 0:
        print("Error running 'hipcc --version'")
        return None

    # Extract the version using a regular expression
    match = re.search(r'HIP version: (\S+)', result.stdout)
    if match:
        # Return the version string
        return match.group(1)
    else:
        print("Could not find HIP version in the output")
        return None


def glob(pattern: str):
    root = Path(__name__).parent
    return [str(p) for p in root.glob(pattern)]


def get_neuronxcc_version():
    import sysconfig
    site_dir = sysconfig.get_paths()["purelib"]
    version_file = os.path.join(site_dir, "neuronxcc", "version",
                                "__init__.py")

    # Check if the command was executed successfully
    with open(version_file, "rt") as fp:
        content = fp.read()

    # Extract the version using a regular expression
    match = re.search(r"__version__ = '(\S+)'", content)
    if match:
        # Return the version string
        return match.group(1)
    else:
        raise RuntimeError("Could not find HIP version in the output")


def get_nvcc_cuda_version(cuda_dir: str) -> Version:
    """Get the CUDA version from nvcc.

    Adapted from https://github.com/NVIDIA/apex/blob/8b7a1ff183741dd8f9b87e7bafd04cfde99cea28/setup.py
    """
    nvcc_output = subprocess.check_output([cuda_dir + "/bin/nvcc", "-V"],
                                          universal_newlines=True)
    output = nvcc_output.split()
    release_idx = output.index("release") + 1
    nvcc_cuda_version = parse(output[release_idx].split(",")[0])
    return nvcc_cuda_version


def get_pytorch_rocm_arch() -> Set[str]:
    """Get the cross section of Pytorch, and aphrodite supported gfx arches

    ROCM can get the supported gfx architectures in one of two ways
    Either through the PYTORCH_ROCM_ARCH env var, or output from
    rocm_agent_enumerator.

    In either case we can generate a list of supported arch's and
    cross reference with APHRODITE's own ROCM_SUPPORTED_ARCHs.
    """
    env_arch_list = os.environ.get("PYTORCH_ROCM_ARCH", None)

    # If we don't have PYTORCH_ROCM_ARCH specified pull the list from
    # rocm_agent_enumerator
    if env_arch_list is None:
        command = "rocm_agent_enumerator"
        env_arch_list = subprocess.check_output([command]).decode('utf-8')\
                        .strip().replace("\n", ";")
        arch_source_str = "rocm_agent_enumerator"
    else:
        arch_source_str = "PYTORCH_ROCM_ARCH env variable"

    # List are separated by ; or space.
    pytorch_rocm_arch = set(env_arch_list.replace(" ", ";").split(";"))

    # Filter out the invalid architectures and print a warning.
    arch_list = pytorch_rocm_arch.intersection(ROCM_SUPPORTED_ARCHS)

    # If none of the specified architectures are valid, raise an error.
    if not arch_list:
        raise RuntimeError(
            f"None of the ROCM architectures in {arch_source_str} "
            f"({env_arch_list}) is supported. "
            f"Supported ROCM architectures are: {ROCM_SUPPORTED_ARCHS}.")
    invalid_arch_list = pytorch_rocm_arch - ROCM_SUPPORTED_ARCHS
    if invalid_arch_list:
        warnings.warn(
            f"Unsupported ROCM architectures ({invalid_arch_list}) are "
            f"excluded from the {arch_source_str} output "
            f"({env_arch_list}). Supported ROCM architectures are: "
            f"{ROCM_SUPPORTED_ARCHS}.",
            stacklevel=2)
    return arch_list


def get_torch_arch_list() -> Set[str]:
    # TORCH_CUDA_ARCH_LIST can have one or more architectures,
    # e.g. "8.0" or "7.5,8.0,8.6+PTX". Here, the "8.6+PTX" option asks the
    # compiler to additionally include PTX code that can be runtime-compiled
    # and executed on the 8.6 or newer architectures. While the PTX code will
    # not give the best performance on the newer architectures, it provides
    # forward compatibility.
    env_arch_list = os.environ.get("TORCH_CUDA_ARCH_LIST", None)
    if env_arch_list is None:
        return set()

    # List are separated by ; or space.
    torch_arch_list = set(env_arch_list.replace(" ", ";").split(";"))
    if not torch_arch_list:
        return set()

    # Filter out the invalid architectures and print a warning.
    valid_archs = NVIDIA_SUPPORTED_ARCHS.union(
        {s + "+PTX"
         for s in NVIDIA_SUPPORTED_ARCHS})
    arch_list = torch_arch_list.intersection(valid_archs)
    # If none of the specified architectures are valid, raise an error.
    if not arch_list:
        raise RuntimeError(
            "None of the CUDA architectures in `TORCH_CUDA_ARCH_LIST` env "
            f"variable ({env_arch_list}) is supported. "
            f"Supported CUDA architectures are: {valid_archs}.")
    invalid_arch_list = torch_arch_list - valid_archs
    if invalid_arch_list:
        warnings.warn(
            f"Unsupported CUDA architectures ({invalid_arch_list}) are "
            "excluded from the `TORCH_CUDA_ARCH_LIST` env variable "
            f"({env_arch_list}). Supported CUDA architectures are: "
            f"{valid_archs}.",
            stacklevel=2)
    return arch_list


if _is_hip():
    rocm_arches = get_pytorch_rocm_arch()
    NVCC_FLAGS += ["--offload-arch=" + arch for arch in rocm_arches]
else:
    # First, check the TORCH_CUDA_ARCH_LIST environment variable.
    compute_capabilities = get_torch_arch_list()

if _is_cuda() and not compute_capabilities:
    # If TORCH_CUDA_ARCH_LIST is not defined or empty, target all available
    # GPUs on the current machine.
    device_count = torch.cuda.device_count()
    for i in range(device_count):
        major, minor = torch.cuda.get_device_capability(i)
        if major < 6 or (major == 6 and minor < 1):
            raise RuntimeError(
                "GPUs with compute capability below 6.1 are not supported.")
        compute_capabilities.add(f"{major}.{minor}")

ext_modules = []

if _is_cuda():
    nvcc_cuda_version = get_nvcc_cuda_version(CUDA_HOME)
    if not compute_capabilities:
        # If no GPU is specified nor available, add all supported architectures
        # based on the NVCC CUDA version.
        compute_capabilities = NVIDIA_SUPPORTED_ARCHS.copy()
        if nvcc_cuda_version < Version("11.1"):
            compute_capabilities.remove("8.6")
        if nvcc_cuda_version < Version("11.8"):
            compute_capabilities.remove("8.9")
            compute_capabilities.remove("9.0")
    # Validate the NVCC CUDA version.
    if nvcc_cuda_version < Version("11.0"):
        raise RuntimeError(
            "CUDA 11.0 or higher is required to build the package.")
    if (nvcc_cuda_version < Version("11.1")
            and any(cc.startswith("8.6") for cc in compute_capabilities)):
        raise RuntimeError(
            "CUDA 11.1 or higher is required for compute capability 8.6.")
    if nvcc_cuda_version < Version("11.8"):
        if any(cc.startswith("8.9") for cc in compute_capabilities):
            # CUDA 11.8 is required to generate the code targeting compute
            # capability 8.9. However, GPUs with compute capability 8.9 can
            # also run the code generated by the previous versions of CUDA 11
            # and targeting compute capability 8.0. Therefore, if CUDA 11.8 is
            # not available, we target compute capability 8.0 instead of 8.9.
            warnings.warn(
                "CUDA 11.8 or higher is required for compute capability 8.9. "
                "Targeting compute capability 8.0 instead.",
                stacklevel=2)
            compute_capabilities = set(cc for cc in compute_capabilities
                                       if not cc.startswith("8.9"))
            compute_capabilities.add("8.0+PTX")
        if any(cc.startswith("9.0") for cc in compute_capabilities):
            raise RuntimeError(
                "CUDA 11.8 or higher is required for compute capability 9.0.")

    NVCC_FLAGS_PUNICA = NVCC_FLAGS.copy()

    # Add target compute capabilities to NVCC flags.
    for capability in compute_capabilities:
        num = capability[0] + capability[2]
        NVCC_FLAGS += ["-gencode", f"arch=compute_{num},code=sm_{num}"]
        if capability.endswith("+PTX"):
            NVCC_FLAGS += [
                "-gencode", f"arch=compute_{num},code=compute_{num}"
            ]
        if int(capability[0]) >= 8:
            NVCC_FLAGS_PUNICA += [
                "-gencode", f"arch=compute_{num},code=sm_{num}"
            ]
            if capability.endswith("+PTX"):
                NVCC_FLAGS_PUNICA += [
                    "-gencode", f"arch=compute_{num},code=compute_{num}"
                ]

    # Use NVCC threads to parallelize the build.
    if nvcc_cuda_version >= Version("11.2"):
        nvcc_threads = int(os.getenv("NVCC_THREADS", 8))
        num_threads = min(os.cpu_count(), nvcc_threads)
        NVCC_FLAGS += ["--threads", str(num_threads)]

    if nvcc_cuda_version >= Version("11.8"):
        NVCC_FLAGS += ["-DENABLE_FP8_E5M2"]

    # changes for punica kernels
    NVCC_FLAGS += torch_cpp_ext.COMMON_NVCC_FLAGS
    REMOVE_NVCC_FLAGS = [
        '-D__CUDA_NO_HALF_OPERATORS__',
        '-D__CUDA_NO_HALF_CONVERSIONS__',
        '-D__CUDA_NO_BFLOAT16_CONVERSIONS__',
        '-D__CUDA_NO_HALF2_OPERATORS__',
    ]
    for flag in REMOVE_NVCC_FLAGS:
        with contextlib.suppress(ValueError):
            torch_cpp_ext.COMMON_NVCC_FLAGS.remove(flag)

    install_punica = bool(
        int(os.getenv("APHRODITE_INSTALL_PUNICA_KERNELS", "1")))
    device_count = torch.cuda.device_count()
    for i in range(device_count):
        major, minor = torch.cuda.get_device_capability(i)
        if major < 8:
            install_punica = False
            break
    if install_punica:
        ext_modules.append(
            CUDAExtension(
                name="aphrodite._punica_C",
                sources=["kernels/punica/punica_ops.cc"] +
                glob("kernels/punica/bgmv/*.cu"),
                extra_compile_args={
                    "cxx": CXX_FLAGS,
                    "nvcc": NVCC_FLAGS_PUNICA,
                },
            ))

    install_hadamard = bool(
        int(os.getenv("APHRODITE_INSTALL_HADAMARD_KERNELS", "1")))
    device_count = torch.cuda.device_count()
    for i in range(device_count):
        major, minor = torch.cuda.get_device_capability(i)
        if major < 7:
            install_hadamard = False
            break
    if install_hadamard:
        ext_modules.append(
            CUDAExtension(
                name="aphrodite._hadamard_C",
                sources=[
                    "kernels/hadamard/fast_hadamard_transform.cpp",
                    "kernels/hadamard/fast_hadamard_transform_cuda.cu"
                ],
                extra_compile_args={
                    "cxx": CXX_FLAGS,
                    "nvcc": NVCC_FLAGS,
                },
            ))

elif _is_neuron():
    neuronxcc_version = get_neuronxcc_version()

aphrodite_extension_sources = [
    "kernels/cache_kernels.cu",
    "kernels/attention/attention_kernels.cu",
    "kernels/pos_encoding_kernels.cu",
    "kernels/activation_kernels.cu",
    "kernels/layernorm_kernels.cu",
    "kernels/quantization/squeezellm/quant_cuda_kernel.cu",
    "kernels/quantization/gguf/gguf_kernel.cu",
    "kernels/quantization/gptq/q_gemm.cu",
    "kernels/quantization/exl2/q_matrix.cu",
    "kernels/quantization/exl2/q_gemm_exl2.cu",
    "kernels/cuda_utils_kernels.cu",
    "kernels/moe/align_block_size_kernel.cu",
    "kernels/pybind.cpp",
]

if _is_cuda():
    aphrodite_extension_sources.append(
        "kernels/quantization/awq/gemm_kernels.cu")
    aphrodite_extension_sources.append(
        "kernels/quantization/quip/origin_order.cu")
    aphrodite_extension_sources.append(
        "kernels/quantization/marlin/marlin_cuda_kernel.cu")
    aphrodite_extension_sources.append(
        "kernels/all_reduce/custom_all_reduce.cu")
    aphrodite_extension_sources.append(
        "kernels/quantization/aqlm/aqlm_cuda_entry.cpp")
    aphrodite_extension_sources.append(
        "kernels/quantization/aqlm/aqlm_cuda_kernel.cu")
    aphrodite_extension_sources.append(
        "kernels/quantization/bitsandbytes/int4_fp16_gemm_kernels.cu")
    aphrodite_extension_sources.append(
        "kernels/quantization/bitsandbytes/format.cu")
    aphrodite_extension_sources.append(
        "kernels/quantization/bitsandbytes/gemm_s4_f16.cu")

    ext_modules.append(
        CUDAExtension(
            name="aphrodite._moe_C",
            sources=glob("kernels/moe/*.cu") + glob("kernels/moe/*.cpp"),
            extra_compile_args={
                "cxx": CXX_FLAGS,
                "nvcc": NVCC_FLAGS,
            },
        ))

if not _is_neuron():
    aphrodite_extension = CUDAExtension(
        name="aphrodite._C",
        sources=aphrodite_extension_sources,
        extra_compile_args={
            "cxx": CXX_FLAGS,
            "nvcc": NVCC_FLAGS,
        },
        libraries=[
            "cuda", "conda/envs/aphrodite-runtime/lib",
            "conda/envs/aphrodite-runtime/lib/stubs"
        ] if _is_cuda() else [],
        library_dirs=[
            "conda/envs/aphrodite-runtime/lib",
            "conda/envs/aphrodite-runtime/lib/stubs"
        ] if _is_cuda() else [],
    )
    ext_modules.append(aphrodite_extension)


def get_path(*filepath) -> str:
    return os.path.join(ROOT_DIR, *filepath)


def find_version(filepath: str) -> str:
    """Extract version information from the given filepath.

    Adapted from https://github.com/ray-project/ray/blob/0b190ee1160eeca9796bc091e07eaebf4c85b511/python/setup.py
    """
    with open(filepath) as fp:
        version_match = re.search(r"^__version__ = ['\"]([^'\"]*)['\"]",
                                  fp.read(), re.M)
        if version_match:
            return version_match.group(1)
        raise RuntimeError("Unable to find version string.")


def get_aphrodite_version() -> str:
    version = find_version(get_path("aphrodite", "__init__.py"))

    if _is_cuda():
        cuda_version = str(nvcc_cuda_version)
        if cuda_version != MAIN_CUDA_VERSION:
            cuda_version_str = cuda_version.replace(".", "")[:3]
            version += f"+cu{cuda_version_str}"
    elif _is_hip():
        # Get the HIP version
        hipcc_version = get_hipcc_rocm_version()
        if hipcc_version != MAIN_CUDA_VERSION:
            rocm_version_str = hipcc_version.replace(".", "")[:3]
            version += f"+rocm{rocm_version_str}"
    elif _is_neuron():
        # Get the Neuron version
        neuron_version = str(neuronxcc_version)
        if neuron_version != MAIN_CUDA_VERSION:
            neuron_version_str = neuron_version.replace(".", "")[:3]
            version += f"+neuron{neuron_version_str}"
    else:
        raise RuntimeError("Unknown environment. Only "
                           "CUDA, HIP, and Neuron are supported.")

    return version


def read_readme() -> str:
    """Read the README file if present."""
    p = get_path("README.md")
    if os.path.isfile(p):
        return io.open(get_path("README.md"), "r", encoding="utf-8").read()
    else:
        return ""


def get_requirements() -> List[str]:
    """Get Python package dependencies from requirements.txt."""
    if _is_cuda():
        with open(get_path("requirements.txt")) as f:
            requirements = f.read().strip().split("\n")
        if nvcc_cuda_version <= Version("11.8"):
            for i in range(len(requirements)):
                if requirements[i].startswith("cupy-cuda12x"):
                    requirements[i] = "cupy-cuda11x"
                    break
    elif _is_hip():
        with open(get_path("requirements-rocm.txt")) as f:
            requirements = f.read().strip().split("\n")
    elif _is_neuron():
        with open(get_path("requirements-neuron.txt")) as f:
            requirements = f.read().strip().split("\n")
    else:
        raise ValueError(
            "Unsupported platform, please use CUDA, ROCm or Neuron.")
    return requirements


package_data = {
    "aphrodite": [
        "endpoints/kobold/klite.embd",
        "modeling/layers/quantization/hadamard.safetensors", "py.typed",
        "modeling/layers/fused_moe/configs/*.json"
    ]
}
if os.environ.get("APHRODITE_USE_PRECOMPILED"):
    ext_modules = []
    package_data["aphrodite"].append("*.so")

setuptools.setup(
    name="aphrodite-engine",
    version=get_aphrodite_version(),
    author="PygmalionAI",
    license="AGPL 3.0",
    description="The inference engine for PygmalionAI models",
    long_description=read_readme(),
    long_description_content_type="text/markdown",
    url="https://github.com/PygmalionAI/aphrodite-engine",
    project_urls={
        "Homepage": "https://pygmalion.chat",
        "Documentation": "https://docs.pygmalion.chat",
        "GitHub": "https://github.com/PygmalionAI",
        "Huggingface": "https://huggingface.co/PygmalionAI",
    },
    classifiers=[
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "License :: OSI Approved :: GNU Affero General Public License v3 or later (AGPLv3+)",  # noqa: E501
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
    ],
    packages=setuptools.find_packages(exclude=("kernels", "examples",
                                               "tests")),
    python_requires=">=3.8",
    install_requires=get_requirements(),
    ext_modules=ext_modules,
    cmdclass={"build_ext": BuildExtension} if not _is_neuron() else {},
    include_package_data=True,
)
