# msrun.tcl — write a my-system run log (msrun.jsonl, format v1) from Tcl / Vivado / Vitis.
# Spec: ~/My_System/runlog/RUNLOG_SPEC.md      Copy into a project: ms log template tcl > msrun.tcl
#
#   source msrun.tcl
#   msrun::start s18_doe3 {seed 3 strategy Performance_Explore}
#   msrun::stage synth
#   synth_design -top top -part xc7z020clg400-1
#   msrun::metric lut 41233
#   msrun::stage route
#   msrun::metric wns_ns [get_property SLACK [get_timing_paths -max_paths 1 -setup]] ns
#   msrun::artifact reports/timing.rpt report
#   msrun::end ok
#
# Errors: wrap the flow in `if {[catch {...} err]} { msrun::fail $err }`.

namespace eval msrun {
    variable fh ""
    variable run ""
    variable t0 0
    variable cur ""
    variable folder ""

    proc jstr {s} {
        set m [list \\ \\\\ \" \\\" \n \\n \r \\r \t \\t]
        set s [string map $m $s]
        regsub -all {[\x00-\x1f]} $s {} s
        return "\"$s\""
    }
    proc jval {v} {
        if {[string is double -strict $v] && ![regexp {^0[0-9]} $v]} { return $v }
        if {$v eq "true" || $v eq "false"} { return $v }
        return [jstr $v]
    }
    proc now {} {
        set t [clock format [clock seconds] -format "%Y-%m-%dT%H:%M:%S%z"]
        return [regsub {([+-][0-9][0-9])([0-9][0-9])$} $t {\1:\2}]
    }
    proc w {ev pairs} {
        variable fh
        variable run
        if {$fh eq ""} { error "msrun: call msrun::start first" }
        set line "\{\"v\":1,\"ts\":[jstr [now]],\"run\":[jstr $run],\"ev\":[jstr $ev]"
        foreach {k v} $pairs {
            if {$v eq "" && $k ne "msg"} { continue }
            if {[string index $k 0] eq "@"} {
                append line ",[jstr [string range $k 1 end]]:$v"
            } else {
                append line ",[jstr $k]:[jstr $v]"
            }
        }
        append line "\}"
        puts $fh $line
        flush $fh
    }
    proc start {name {params {}} {folder ""}} {
        variable fh
        variable run
        variable t0
        variable cur
        set dir [expr {$folder eq "" ? [pwd] : [file normalize $folder]}]
        set [namespace current]::folder $dir
        file mkdir $dir
        set run "$name-[clock format [clock seconds] -format %Y%m%dT%H%M%S]-[format %04x [expr {int(rand()*65536)}]]"
        set path [file join $dir msrun.jsonl]
        set fh [open $path a]
        fconfigure $fh -encoding utf-8
        set t0 [clock milliseconds]
        set cur ""
        set p "\{"
        set first 1
        foreach {k v} $params {
            if {!$first} { append p "," }
            append p "[jstr $k]:[jval $v]"
            set first 0
        }
        append p "\}"
        set cmd "[info nameofexecutable] [expr {[info exists ::argv0] ? $::argv0 : {}}]"
        w start [list name $name project [file tail $dir] cmd $cmd cwd $dir host [info hostname] @pid [pid] @params $p]
        # register with my-system (best effort)
        set base [expr {[info exists ::env(MY_SYSTEM)] ? $::env(MY_SYSTEM) : [file join $::env(HOME) My_System]}]
        if {[file isdirectory [file join $base reports]]} {
            catch {
                set ix [open [file join $base reports runlog_index.tsv] a]
                puts $ix "[file normalize $path]\t$run\t[clock seconds]"
                close $ix
            }
        }
        return $run
    }
    proc end_stage {{status ok}} {
        variable cur
        if {$cur ne ""} {
            w stage [list stage $cur state end status $status]
            set cur ""
        }
    }
    proc stage {name {status ok}} {
        variable cur
        end_stage $status
        set cur $name
        w stage [list stage $name state begin]
    }
    proc log {level msg} {
        variable cur
        set level [string toupper $level]
        if {$level ni {DEBUG INFO WARN ERROR}} { error "msrun::log level must be DEBUG INFO WARN ERROR" }
        w log [list level $level msg $msg stage $cur]
    }
    proc metric {name value {unit ""}} {
        variable cur
        if {![regexp {^[a-z][a-z0-9_]*$} $name]} { error "msrun::metric name must be snake_case" }
        w metric [list name $name @value [jval $value] unit $unit stage $cur]
    }
    proc artifact {path {kind other}} {
        variable cur
        if {$kind ni {report bitstream checkpoint netlist plot data log other}} { error "msrun::artifact bad kind" }
        w artifact [list path $path kind $kind stage $cur]
    }
    proc progress {pct {msg ""}} {
        variable cur
        w progress [list @pct $pct msg $msg stage $cur]
    }
    proc end {{status ok} {error ""}} {
        variable fh
        variable t0
        if {$fh eq ""} { return }
        end_stage [expr {$status eq "ok" ? "ok" : "failed"}]
        set secs [format %.1f [expr {([clock milliseconds] - $t0) / 1000.0}]]
        set rc [expr {$status eq "ok" ? 0 : 1}]
        if {$status eq "failed" && $error eq ""} { set error failed }
        w end [list status $status @rc $rc @seconds $secs error $error]
        close $fh
        set fh ""
    }
    proc fail {err} {
        log ERROR $err
        end failed $err
    }
}
