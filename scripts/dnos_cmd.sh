#!/usr/bin/expect -f
set timeout 30
set host "WKY1C7VD00008P2"
set user "dnroot"
set pass "dnroot"

# Get commands from arguments or stdin
if {[llength $argv] > 0} {
    set cmds [lindex $argv 0]
} else {
    set cmds "show version"
}

log_user 1

spawn sshpass -p $pass ssh -tt -o StrictHostKeyChecking=no -o PreferredAuthentications=password,keyboard-interactive -o PubkeyAuthentication=no $user@$host

expect {
    "#" { }
    ">" { }
    timeout { puts "TIMEOUT waiting for prompt"; exit 1 }
}

foreach cmd [split $cmds "\n"] {
    set cmd [string trim $cmd]
    if {$cmd eq ""} continue
    send "$cmd\r"
    expect {
        -re {#\s*$} { }
        -re {>\s*$} { }
        timeout { puts "TIMEOUT after: $cmd"; exit 1 }
    }
}

send "exit\r"
expect eof
