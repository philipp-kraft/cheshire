# Cheshire Simulation Setup Notes

## Fixes required to get simulation running

### 1. RISC-V toolchain not in PATH

`riscv64-unknown-elf-gcc` is not in the default PATH. The toolchain is at
`/usr/pack/riscv-1.0-kgf/riscv64-gcc-11.2.0/bin/`.

Add it permanently to `~/.tcshrc`:

```tcsh
setenv PATH /usr/pack/riscv-1.0-kgf/riscv64-gcc-11.2.0/bin:$PATH
```

### 2. Setu python dependencies 
```
uv sync
source .venv/bin/activate.csh
```

### 3. Default QuestaSim version too old

Use a newer version: `questa-2026.1` is available on
this machine:

```bash
cd target/sim/vsim
questa-2026.1 vsim -c -do run.tcl
```

## Running a simulation

```tcl
# target/sim/vsim/run.tcl
set BINARY ../../../sw/tests/helloworld.spm.elf
set BOOTMODE 0
set PRELMODE 1

source compile.cheshire_soc.tcl
source start.cheshire_soc.tcl
run -all
```

```bash
cd target/sim/vsim
questa-2026.1 vsim -c -do run.tcl
```
