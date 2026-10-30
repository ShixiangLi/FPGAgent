set root [file normalize [file join [file dirname [info script]] ..]]
set out [file join $root build vivado]
create_project led_runner $out -part xczu15eg-ffvb1156-2-i -force
add_files [glob [file join $root rtl *.sv]]
add_files -fileset constrs_1 [file join $root pins axu15egb.xdc]
set_property top led_top [current_fileset]
set_property verilog_define HARDWARE_DEBUG [current_fileset]
create_ip -name vio -vendor xilinx.com -library ip -module_name vio_led
set_property -dict [list CONFIG.C_NUM_PROBE_IN {2} CONFIG.C_NUM_PROBE_OUT {0} CONFIG.C_PROBE_IN0_WIDTH {2} CONFIG.C_PROBE_IN1_WIDTH {1} CONFIG.C_EN_PROBE_IN_ACTIVITY {0}] [get_ips vio_led]
set_property generate_synth_checkpoint false [get_files vio_led.xci]
generate_target all [get_ips vio_led]
update_compile_order -fileset sources_1
synth_design -top led_top -part xczu15eg-ffvb1156-2-i
write_checkpoint -force [file join $out synthesized.dcp]
opt_design
place_design
phys_opt_design
route_design
write_checkpoint -force [file join $out routed.dcp]
report_timing_summary -file [file join $out timing_summary.rpt]
report_drc -file [file join $out drc.rpt]
report_utilization -file [file join $out utilization.rpt]
report_bus_skew -file [file join $out bus_skew.rpt]
report_io -file [file join $out io.rpt]
set errors 0
foreach violation [get_drc_violations] {
    if {[get_property SEVERITY $violation] in {Error {Critical Warning}}} {incr errors}
}
if {$errors > 0} {error "DRC has $errors blocking violations"}
# Check the summary rows of the vendor debug-core bus-skew constraints.
set skew_report [report_bus_skew -return_string]
set skew_count 0
foreach line [split $skew_report "\n"] {
    if {[regexp {^\s*(Slow|Fast)\s+([0-9.]+)\s+([0-9.]+)\s+(-?[0-9.]+)\s*$} $line -> corner requirement actual slack]} {
        incr skew_count
        if {$slack < 0} {error "Bus skew violation: $line"}
    }
}
if {$skew_count == 0} {error "Cannot verify debug-core bus skew constraints"}
set checks [open [file join $out checks.json] w]
puts $checks "{\"drc_errors\": $errors, \"bus_skew_passed\": true}"
close $checks
set paths [get_timing_paths -max_paths 1]
if {[llength $paths] == 0 || [get_property SLACK $paths] < 0} {error "Setup timing failed"}
set paths [get_timing_paths -delay_type min -max_paths 1]
if {[llength $paths] > 0 && [get_property SLACK $paths] < 0} {error "Hold timing failed"}
write_debug_probes -force [file join $out led_top.ltx]
write_bitstream -force [file join $out led_top.bit]
puts "BUILD PASS"
