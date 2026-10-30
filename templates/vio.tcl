# Vivado may split one VIO input port into several named net fragments.
# Describe and validate the physical port bits before reading any samples.
proc agent_vio_layout {vio widths} {
    set layout {}
    for {set port 0} {$port < [llength $widths]} {incr port} {
        set expected [lindex $widths $port]
        set covered 0
        set parts {}
        foreach probe [get_hw_probes -of_objects $vio -filter "PROBE_PORT == $port && TYPE == vio_input"] {
            set offset [get_property PROBE_PORT_BITS $probe]
            set width [get_property WIDTH $probe]
            set count [get_property PROBE_PORT_BIT_COUNT $probe]
            if {$offset < 0 || $width < 1 || $width != $count || $offset+$width > $expected} {
                error "VIO port $port invalid fragment $probe: offset=$offset width=$width count=$count expected=$expected"
            }
            set mask [expr {((1 << $width)-1) << $offset}]
            if {$covered & $mask} {error "VIO port $port overlapping fragment $probe at offset $offset width $width"}
            set covered [expr {$covered | $mask}]
            set_property INPUT_VALUE_RADIX HEX $probe
            lappend parts [list $probe $offset $width]
        }
        if {$covered != (1 << $expected)-1} {
            error "VIO port $port incomplete mapping: expected $expected bits, covered mask [format %x $covered], fragments $parts"
        }
        lappend layout $parts
    }
    return $layout
}

proc agent_vio_value {parts} {
    set value 0
    foreach part $parts {
        lassign $part probe offset width
        set raw [get_property INPUT_VALUE $probe]
        if {![regexp {^[0-9a-fA-F]+$} $raw] || [scan $raw %x chunk] != 1 || $chunk >= (1 << $width)} {
            error "VIO fragment $probe invalid hexadecimal value '$raw' for width $width"
        }
        set value [expr {$value | ($chunk << $offset)}]
    }
    return [format %x $value]
}
