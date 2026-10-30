set root [file normalize [file join [file dirname [info script]] ..]]
source [file join $root scripts project_data.tcl]
set out [file join $root build vivado]
open_hw_manager
connect_hw_server -allow_non_jtag
set targets [get_hw_targets]
if {[llength $targets] != 1} {error "Expected exactly one JTAG target"}
current_hw_target [lindex $targets 0]
set_property PARAM.FREQUENCY 10000000 [current_hw_target]
open_hw_target
set devices [get_hw_devices -filter {PART =~ xczu15*}]
if {[llength $devices] != 1} {error "Expected one XCZU15 device"}
set dev [lindex $devices 0]
current_hw_device $dev
set_property PROGRAM.FILE [file join $out design.bit] $dev
if {$agent_debug} {
    set_property PROBES.FILE [file join $out design.ltx] $dev
    set_property FULL_PROBES.FILE [file join $out design.ltx] $dev
}
program_hw_devices $dev
refresh_hw_device $dev
if {$agent_debug} {
    set vios [get_hw_vios -of_objects $dev]
    if {[llength $vios] != 1} {error "Expected one agent VIO"}
    set vio [lindex $vios 0]
    set probes {}
    for {set i 0} {$i < [llength $agent_probe_names]} {incr i} {
        set probe [get_hw_probes -of_objects $vio -filter "PROBE_PORT == $i"]
        if {[llength $probe] != 1 || [get_property WIDTH $probe] != [lindex $agent_probe_widths $i]} {error "VIO probe mismatch"}
        set_property INPUT_VALUE_RADIX HEX $probe
        lappend probes $probe
    }
    set f [open [file join $root build hardware_samples.csv] w]
    puts $f "elapsed_ms,[join $agent_probe_names ,]"
    set start [clock milliseconds]
    for {set sample 0} {$sample < $agent_samples} {incr sample} {
        refresh_hw_vio $vio
        set values [list [expr {[clock milliseconds]-$start}]]
        foreach probe $probes {lappend values [get_property INPUT_VALUE $probe]}
        puts $f [join $values ,]
        flush $f
        after $agent_sample_ms
    }
    close $f
}
close_hw_target
disconnect_hw_server
close_hw_manager
puts "PROGRAM PASS"
