# llama.cpp on NVIDIA DGX Spark: build and run guide

Guide for getting llama.cpp compiled with CUDA on a DGX Spark (GB10, 128 GB unified memory) and serving models over an OpenAI compatible API.

## DGX Spark is different 

The Spark runs a GB10 chip: a Grace CPU (Armv9, aarch64) and a Blackwell GPU that share one big pool of 128 GB memory. The GPU has compute capability 12.1 (sm_121). Two things make this matter for the build:

1. The CUDA architecture flag. sm_121 is not a desktop Blackwell. To unlock the native FP4 and MXFP4 tensor core instructions you need the `a` suffix, meaning `121a` rather than plain `121`. The good news: llama.cpp's own CMake already rewrites a bare `121` into `121a` for you (more on that below).
2. Everything is Arm. The binaries are aarch64, and the CPU side gets Armv9 vector extensions, so native CPU tuning helps.

## 1. Install the dependencies

```bash
sudo apt update
sudo apt install -y git clang cmake libcurl4-openssl-dev libssl-dev
```

The NVIDIA playbook keeps it to this list. `clang` and `cmake` are the compiler and build system, `libcurl4-openssl-dev` and `libssl-dev` give llama.cpp HTTPS support so it can pull models straight from Hugging Face. Optionally add `ccache` so rebuilds are fast:

```bash
sudo apt install -y ccache
```

## 2. Clone the repository

```bash
git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp
cd ~/llama.cpp
```

Use the upstream `ggml-org/llama.cpp`. There used to be a popular fork for the Spark (`croll83/llama.cpp-dgx`) but it is deprecated since May 2026, upstream has surpassed it.

## 3. Configure CMake

There are two commands that matter for the Spark. One is from NVIDIA's official playbook, the other is what was actually used to compile llama.cpp on the work Spark. Both build fine and both end up with the Blackwell optimizations.

### NVIDIA's official command

```bash
cmake -B build -DGGML_NATIVE=ON -DGGML_CUDA=ON -DGGML_CURL=ON -DGGML_RPC=ON -DCMAKE_CUDA_ARCHITECTURES=121a-real
```

### The command used on the work Spark

This is what was actually run on the work Spark to compile it:

```bash
cmake -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_CUDA=ON \
  -DGGML_CUDA_FA=ON \
  -DGGML_CUDA_GRAPHS=ON \
  -DGGML_CUDA_NCCL=ON \
  -DGGML_CUDA_COMPRESSION_MODE=size \
  -DCMAKE_CUDA_ARCHITECTURES=121
cmake --build build --config Release -j$(nproc)
```

### How they compare

| Flag | What it does | NVIDIA playbook | Work Spark command |
|---|---|---|---|
| `-DGGML_CUDA=ON` | CUDA backend, offload to the GB10 GPU | Yes | Yes |
| `-DCMAKE_CUDA_ARCHITECTURES=121a-real` | Native sm_121a, device code only | Yes | No, uses `121` |
| `-DCMAKE_CUDA_ARCHITECTURES=121` | sm_121, auto rewritten to 121a by CMake | No | Yes |
| `-DGGML_NATIVE=ON` | CPU kernels tuned for the local CPU | Yes | If not cross-compiling, its ON by default |
| `-DGGML_CURL=ON` | Enables `-hf` Hugging Face download | Yes | No |
| `-DGGML_RPC=ON` | RPC backend for remote inference | Yes | No, if you want to pool remote GPUs together, set it to ON |
| `-DGGML_CUDA_FA=ON` | Flash attention (default anyway) | Implied | Explicit |
| `-DGGML_CUDA_GRAPHS=ON` | CUDA graph capture, cuts kernel launch overhead | No | Yes |
| `-DGGML_CUDA_NCCL=ON` | NCCL multi-GPU support, not the same as RPC | No | Yes, cosmetic for 1 spark |
| `-DGGML_CUDA_COMPRESSION_MODE=size` | nvcc `-compress-mode=size`, smaller binaries | No | Yes |
| `-DCMAKE_BUILD_TYPE=Release` | Release build, compiler optimizations | Implied by `--config Release` | Explicit |

A few notes on the differences:

- `121` gets rewritten to `121a` by llama.cpp's CMake automatically (see the section below), so both commands end up with the Blackwell FP4 tensor cores. The work Spark command just builds both PTX and device code instead of device code only.
- `-DGGML_CUDA_FA=ON` is the default in current llama.cpp, so writing it is harmless but redundant. The same goes for `-DCMAKE_BUILD_TYPE=Release` since the build step already passes `--config Release`.
- `-DGGML_CUDA_GRAPHS=ON` enables CUDA graph capture for the compute graph, which reduces per-launch overhead. Worth having on a single GPU box.
- `-DGGML_CUDA_NCCL=ON` only matters if you ever plan to run multiple GPUs. On a single Spark it does nothing at runtime, but it compiles cleanly if NCCL is installed.
- `-DGGML_CUDA_COMPRESSION_MODE=size` tells nvcc to compress fatbins for size rather than speed. The tradeoff is smaller binaries against slightly slower JIT loading. It was added in CUDA 12.8 and llama.cpp only applies it for that toolkit version or newer.


### What each flag does

| Flag | What it does | Needed? |
|---|---|---|
| `-DGGML_CUDA=ON` | Enables the CUDA backend so matrix ops and transformer layers offload to the GB10 GPU | Yes....... |
| `-DCMAKE_CUDA_ARCHITECTURES=121a-real` | Compiles for sm_121a natively, no PTX JIT. The `a` unlocks Blackwell FP4/MXFP4 tensor cores | Yes, GB10 specific, auto fixed |
| `-DGGML_NATIVE=ON` | Tunes CPU kernels for the local CPU (Grace Armv9) | Recommended, NVIDIA ships it |
| `-DGGML_CURL=ON` | Enables `-hf` model auto-download from Hugging Face | Only if you want the `-hf` workflow, manual downlaod gives more control   |
| `-DGGML_RPC=ON` | RPC backend for remote inference | Optional, NVIDIA includes it |
| `-DGGML_CUDA_F16=ON` | FP16 CUDA kernels, less memory and more throughput on quantized models | Recommended by the Arm guide, could be on by default? But the work spark does not include it |
| `-DGGML_CUDA_FA_ALL_QUANTS=ON` | Flash attention for all quant types | Recommended on Blackwell, we dont use it |
| `-DBUILD_SHARED_LIBS=OFF` | Static build | Only if you move binaries around |
| `-DGGML_CCACHE=OFF` | Silences the ccache warning | Cosmetic, install ccache instead |

### Why `121a-real` and not `121`

Both work, but for slightly different reasons. Passing `121` relies on llama.cpp's CMake to fix it for you. In `ggml/src/ggml-cuda/CMakeLists.txt` there is a block that rewrites any plain `12X` architecture into `12Xa`:

```cmake
# Replace any plain 12X CUDA architectures with their "architecture-specific" equivalents 12Xa.
# 12X is forwards-compatible, 12Xa is not.
# Notably the Blackwell FP4 tensor core instructions are not forwards compatible and therefore need 12Xa.
```

So `121` becomes `121a` automatically, and you see a `Replacing 121 in CMAKE_CUDA_ARCHITECTURES with 121a` status line during configure. Passing `121a-real` yourself just skips the rewrite and builds device code only, which gives a smaller binary. Either way you get the Blackwell optimizations.

### A known compile bug to be aware of

In December 2025 a PR adding native MXFP4 support broke the CUDA build for compute capability 121. The problem was a ptxas error at about 28% build progress:

```text
ptxas fatal: Instruction 'mma with block scale' not supported on .target 'sm_121'
```

The offending PR was reverted the same day and the issue is closed (ggml-org/llama.cpp #18425). It has been broken and fixed twice, so if it ever shows up again the workarounds are `-DGGML_NATIVE=OFF` or `-DCMAKE_CUDA_ARCHITECTURES=120f`. The advice from the forums is to delete the build directory before every rebuild:

```bash
rm -rf build
git pull
cmake -B build ...
```

## 4. Build

```bash
cmake --build build --config Release --target llama-server -j
```

 `llama-server` lands in `build/bin/`. To build everything including `llama-cli`, `llama-quantize` and the rest, drop the `--target`:

```bash
cmake --build build --config Release -j
```

## 5. Verify the build

```bash
./build/bin/llama-server --version
```

You should see something like:

```text
ggml_cuda_init: found 1 CUDA devices:
  Device 0: NVIDIA GB10, compute capability 12.1, VMM: yes
```



## 6. Run llama-server with a model

The quick way is to let llama.cpp pull a GGUF straight from Hugging Face with `-hf`.
```bash
./build/bin/llama-server \
  -hf hfrepo/model \
  --host 0.0.0.0 \
  --port 30000
```

MTP speculative decoding is a big win on this hardware (the co-trained drafter gets 45 to 85% token acceptance). Add it with:

```bash
./build/bin/llama-server \
  -hf hfrepo/model \
  --host 0.0.0.0 \
  --port 30000 \
  --chat-template-kwargs '{"preserve_thinking": true}' \
  --spec-type draft-mtp \
  --spec-draft-n-max 3
```

Wait for the `server is listening` log line before hitting the API. Large GGUFs can take a minute or more to load.

IMPORTANT!!! Not all models come with the MTP draft model baked in, on some you will see a seperate downlaod check unsloth/gemma4(any version) to see it [https://huggingface.co/unsloth/gemma-4-12b-it-GGUF](https://huggingface.co/unsloth/gemma-4-12b-it-GGUF)

## Useful inference flags

| Flag | What it does |
|---|---|
| `-c, --ctx-size N` | Context size. Default 0 means loaded from the model. Agentic or coding work wants 32768 minimum, ideally 100000 plus |
| `-ngl, --n-gpu-layers N` | Layers to store in VRAM. `auto` or `all` both work well on the Spark, 999 does the same thing as `all` |
| `-fa, --flash-attn on\|off\|auto` | Flash attention, default auto. Leave it |
| `-b, --batch-size N` | Logical max batch size, default 2048, leave it unless you know what you are doing |
| `-ub, --ubatch-size N` | Physical max batch size, default 512, leave it unless you know what you are doing |
| `-np, --parallel N` | Number of parallel sequences - side effect is ctx-size / np = lower ctx per np |
| `-t, --threads N` | CPU threads for generation. Default -1 means autodetect |
| `--temp N` | Sampling temperature, default 0.80, change only if you need something specific |
| `--top-k N` | Top-k sampling, default 40, 0 disables it, change only if you need something specific |
| `--top-p N` | Top-p sampling, default 0.95, change only if you need something specific |
| `--min-p N` | Min-p sampling, default 0.05, change only if you need something specific |
| `--spec-type draft-mtp` | MTP speculative decoding for compatible models |
| `--spec-draft-n-max N` | Draft length for speculative decoding, default 3 |
| `--no-mmap` | Disable memory mapping (deprecated, use `--load-mode`) |
| `--load-mode MODE` | auto, none, mmap, mlock, mmap+mlock, dio |

For the full list run `./build/bin/llama-server --help`.

There are other draft model strategies like dspark, dflash, ngram but compatability per models needs to be checked first

## Using a model.ini file

llama.cpp supports INI based configuration, which is the clean way to run several models with their own settings instead of typing a huge command line every time.

### Router mode with `models.ini`

Start `llama-server` without any model and point it at an INI file with `--models-preset`:

```bash
./build/bin/llama-server --models-preset ./models.ini
```

Each section in the file is a preset. Keys map to command line arguments without the leading dashes. A minimal example:

```ini
version = 1

[*]
c = 262144
n-gpu-layers = all
parallel = 4

[Qwen3.6-35B]
hf = unsloth/Qwen3.6-35B-A3B-MTP-GGUF:UD-Q4_K_XL
batch-size = 2048
ubatch-size = 2048
top-p = 1.0
top-k = 0
min-p = 0.01
temp = 1.0
spec-type = draft-mtp
spec-draft-n-max = 3
chat-template-kwargs = {"preserve_thinking": true}

[custom]
model = /home/me/models/my-model-Q4_K_M.gguf
ctx-size = 32768
```

The `[*]` section holds defaults shared by every preset, and a named section overrides them. Model-specific options beat the global ones, and command line arguments beat everything. File paths are relative to the server's working directory, absolute paths are safer.

The server then routes requests by model name. The OpenAI style body needs a `"model"` field that matches a section or an HF repo id, and llama-server loads the model on demand:

```bash
curl -X POST http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen3.6-35B",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

## Official docs

- Build instructions: [https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md)
- Server documentation: [https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
- INI presets: [https://github.com/ggml-org/llama.cpp/blob/master/docs/preset.md](https://github.com/ggml-org/llama.cpp/blob/master/docs/preset.md)
- NVIDIA DGX Spark llama.cpp playbook: [https://build.nvidia.com/spark/llama-cpp/instructions](https://build.nvidia.com/spark/llama-cpp/instructions)
- Arm Learning Paths GPU build guide: [https://learn.arm.com/learning-paths/laptops-and-desktops/dgx_spark_llamacpp/2_gb10_llamacpp_gpu/](https://learn.arm.com/learning-paths/laptops-and-desktops/dgx_spark_llamacpp/2_gb10_llamacpp_gpu/)
