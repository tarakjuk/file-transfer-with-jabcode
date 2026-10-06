#!/bin/sh
# libjabcode -> WebAssembly (wasi-sdk 25 필요: https://github.com/WebAssembly/wasi-sdk/releases)
# 사용: WASI_SDK=/path/to/wasi-sdk JABCODE_SRC=/path/to/jabcode/src/jabcode ./build.sh
set -e
SDK=${WASI_SDK:-/home/claude/wasi-sdk-25.0-x86_64-linux}
SRC=${JABCODE_SRC:-/home/claude/jabcode/src/jabcode}
OUT=${OUT:-../receiver}
build() {
  $SDK/bin/clang --target=wasm32-wasi --sysroot=$SDK/share/wasi-sysroot -O3 -flto -mexec-model=reactor $1 \
    -I$SRC/include -I$SRC -DNDEBUG -Wno-everything \
    $(ls $SRC/*.c | grep -v image.c) jabwasm.c \
    -Wl,--export=malloc -Wl,--export=free -Wl,--strip-all -Wl,-z,stack-size=2097152 \
    -Wl,--initial-memory=16777216 -o $2
}
build "" $OUT/jabcode.wasm
ls -la $OUT/*.wasm
