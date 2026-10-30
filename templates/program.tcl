set root [file normalize [file join [file dirname [info script]] ..]]
set out [file join $root build vivado]
open_hw_manager
connect_hw_server -allow_non_jtag
set targets [get_hw_targets]
if {[llength $targets] != 1} {error "Expected exactly one JTAG target, found: $targets"}
current_hw_target [lindex $targets 0]
set_property PARAM.FREQUENCY 10000000 [current_hw_target]
open_hw_target
set devices [get_hw_devices -filter {PART =~ xczu15*}]
if {[llength $devices] != 1} {error "Expected one XCZU15 device, found: $devices"}
set dev [lindex $devices 0]
current_hw_device $dev
set_property PROGRAM.FILE [file join $out led_top.bit] $dev
set_property PROBES.FILE [file join $out led_top.ltx] $dev
set_property FULL_PROBES.FILE [file join $out led_top.ltx] $dev
program_hw_devices $dev
refresh_hw_device $dev
set vios [get_hw_vios -of_objects $dev]
if {[llength $vios] != 1} {error "Expected LED VIO monitor, found: $vios"}
set vio [lindex $vios 0]
set probes [get_hw_probes -of_objects $vio]
foreach p $probes {report_property $p}
set ledprobe [get_hw_probes -of_objects $vio -filter {WIDTH == 2}]
set rstprobe [get_hw_probes -of_objects $vio -filter {WIDTH == 1}]
if {[llength $ledprobe] != 1 || [llength $rstprobe] != 1} {error "Unexpected VIO probes"}
set f [open [file join $root build hardware_samples.csv] w]
puts $f "elapsed_ms,led_n_hex,reset_hex"
set start [clock milliseconds]
set seen {}
set transitions 0
set previous ""
set step_ms $::env(FPGA_STEP_MS)
set poll_ms [expr {max(10, min(100, $step_ms / 3))}]
set sample_count [expr {int(ceil(6.0 * $step_ms / $poll_ms)) + 5}]
for {set i 0} {$i < $sample_count} {incr i} {
    refresh_hw_vio $vio
    set led [get_property INPUT_VALUE $ledprobe]
    set rst [get_property INPUT_VALUE $rstprobe]
    set value [expr 0x$led]
    set reset [expr 0x$rst]
    puts $f "[expr {[clock milliseconds]-$start}],$led,$rst"
    flush $f
    if {$reset != 0 || ($value != 1 && $value != 2)} {close $f; error "Invalid hardware sample led=$led reset=$rst"}
    dict set seen $value 1
    if {$previous ne "" && $previous != $value} {incr transitions}
    set previous $value
    after $poll_ms
}
close $f
if {[dict size $seen] != 2 || $transitions < 4} {error "LED output did not alternate sufficiently: $transitions transitions"}
puts "HARDWARE LOGIC PASS: both LED states observed, $transitions transitions; see build/hardware_samples.csv"
close_hw_target
disconnect_hw_server
close_hw_manager
