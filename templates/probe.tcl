open_hw_manager
connect_hw_server -allow_non_jtag
set targets [get_hw_targets]
if {[llength $targets] != 1} {error "Expected exactly one connected JTAG target"}
puts "JTAG_TARGETS: $targets"
foreach target $targets {
    current_hw_target $target
    open_hw_target
    puts "JTAG_DEVICES: [get_hw_devices]"
    if {[llength [get_hw_devices -filter {PART =~ xczu15*}]] != 1} {error "Expected one XCZU15"}
    close_hw_target
}
disconnect_hw_server
close_hw_manager
puts "PROBE PASS"
