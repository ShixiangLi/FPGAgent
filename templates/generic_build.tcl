set root [file normalize [file join [file dirname [info script]] ..]]
source [file join $root scripts project_data.tcl]
set out [file join $root build vivado]
create_project agent_project $out -part $agent_part -force
add_files [lsort [glob -nocomplain [file join $root rtl *.sv] [file join $root rtl *.v]]]
add_files -fileset constrs_1 [file join $root pins board.xdc]
set_property top $agent_top [current_fileset]
if {$agent_debug} {
    set_property verilog_define HARDWARE_DEBUG [current_fileset]
    create_ip -name vio -vendor xilinx.com -library ip -module_name agent_vio
    set properties [list CONFIG.C_NUM_PROBE_IN [llength $agent_probe_names] CONFIG.C_NUM_PROBE_OUT 0 CONFIG.C_EN_PROBE_IN_ACTIVITY 0]
    set i 0
    foreach width $agent_probe_widths {
        lappend properties CONFIG.C_PROBE_IN${i}_WIDTH $width
        incr i
    }
    set_property -dict $properties [get_ips agent_vio]
    set_property generate_synth_checkpoint false [get_files agent_vio.xci]
    generate_target all [get_ips agent_vio]
}
update_compile_order -fileset sources_1
synth_design -top $agent_top -part $agent_part
if {[lsort [get_ports]] ne [lsort $agent_ports]} {error "Top ports do not exactly match verified board bindings"}
foreach port $agent_ports direction $agent_directions {
    set expected [expr {$direction eq "input" ? "IN" : "OUT"}]
    if {[get_property DIRECTION [get_ports $port]] ne $expected} {error "Port direction mismatch: $port"}
}
write_checkpoint -force [file join $out synthesized.dcp]
opt_design
place_design
phys_opt_design
route_design
write_checkpoint -force [file join $out routed.dcp]
report_timing_summary -file [file join $out timing_summary.rpt]
check_timing -verbose -file [file join $out timing_checks.rpt]
report_drc -file [file join $out drc.rpt]
report_utilization -file [file join $out utilization.rpt]
report_io -file [file join $out io.rpt]
set errors 0
foreach violation [get_drc_violations] {
    if {[get_property SEVERITY $violation] in {Error {Critical Warning}}} {incr errors}
}
if {$agent_debug} {
    report_bus_skew -file [file join $out bus_skew.rpt]
    write_debug_probes -force [file join $out design.ltx]
}
set checks [open [file join $out checks.json] w]
puts $checks "{\"drc_errors\": $errors, \"bus_skew_passed\": null, \"debug_enabled\": $agent_debug}"
close $checks
write_bitstream -force [file join $out design.bit]
puts "BUILD PASS"
