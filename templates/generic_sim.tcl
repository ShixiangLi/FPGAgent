set root [file normalize [file join [file dirname [info script]] ..]]
set out [file join $root build sim]
file mkdir $out
cd $out
set data_out [file normalize [file join $out data]]
if {[file dirname $data_out] ne [file normalize $out]} {error "Invalid simulator data path"}
if {[file exists $data_out]} {file delete -force $data_out}
if {[file isdirectory [file join $root data]]} {file copy -force [file join $root data] $data_out}
foreach log {compile.log elaborate.log simulation.log} {file delete -force $log}
set sources [concat [lsort [glob -nocomplain [file join $root rtl *.sv] [file join $root rtl *.v]]] [lsort [glob -nocomplain [file join $root sim *.sv] [file join $root sim *.v]]]]
exec xvlog --sv {*}$sources > compile.log 2>@1
exec xelab agent_sim -L unisims_ver -s agent_snapshot -debug typical > elaborate.log 2>@1
set f [open run.tcl w]
puts $f {log_wave -recursive *
open_vcd waveform.vcd
log_vcd [get_objects -r /agent_sim/*]
run all
close_vcd
quit}
close $f
exec xsim agent_snapshot -tclbatch run.tcl > simulation.log 2>@1
set f [open simulation.log r]
set result [read $f]
close $f
puts $result
if {![regexp -line {^AGENT_SIM_PASS\s*$} $result]} {error "Testbench did not complete successfully"}
