# Preload `coremark.spm.elf` through serial link
set BINARY ../../../sw/tests/coremark.spm.elf
set BOOTMODE 0
set PRELMODE 1

# Compile design
source compile.cheshire_soc.tcl

# Start and run simulation
source start.cheshire_soc.tcl
run -all