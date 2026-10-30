set root [file normalize [file join [file dirname [info script]] ..]]
set out [file join $root build sim]
file mkdir $out
cd $out
exec xvlog --sv [file join $root rtl led_runner.sv] [file join $root rtl led_top.sv] [file join $root sim tb_led.sv] > compile.log 2>@1
exec xelab tb_led -L unisims_ver -s led_sim -debug typical > elaborate.log 2>@1
set f [open run.tcl w]
puts $f {log_wave -recursive *
run all
quit}
close $f
exec xsim led_sim -tclbatch run.tcl > simulation.log 2>@1
set f [open simulation.log r]
set result [read $f]
close $f
puts $result
if {![string match {*SIMULATION PASS*} $result]} {error "Simulation did not pass"}
