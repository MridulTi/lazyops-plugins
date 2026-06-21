#!/usr/bin/env expect

set timeout 20
set config_file "server_passwords.conf"

proc trim {s} {
    regsub {^\s+} $s "" s
    regsub {\s+$} $s "" s
    return $s
}

# Read config values
set username ""
set temp_pass ""
set new_pass ""
set servers {}

set f [open $config_file r]
while {[gets $f line] >= 0} {
    if {[regexp {^\s*#} $line]} continue
    if {[regexp {^\s*$} $line]} continue

    if {[regexp {^username=(.+)$} $line -> u]} {
        set username [trim $u]
        continue
    }
    if {[regexp {^temp_pass=(.+)$} $line -> t]} {
        set temp_pass [trim $t]
        continue
    }
    if {[regexp {^new_pass=(.+)$} $line -> n]} {
        set new_pass [trim $n]
        continue
    }
    if {[regexp {^\[servers\]} $line]} {
        continue
    }

    # Add server IP if line looks like IP or hostname
    if {[regexp {^\S+$} $line]} {
        lappend servers [trim $line]
    }
}
close $f

if {$username eq "" || $temp_pass eq "" || $new_pass eq ""} {
    puts "Please specify username, temp_pass and new_pass in config file"
    exit 1
}

if {[llength $servers] == 0} {
    puts "No servers found in config"
    exit 1
}

foreach ip $servers {
    puts "Changing password for $username on $ip ..."

    spawn ssh $username@$ip

    expect {
        "yes/no" {
            send "yes\r"
            expect "password:"
            send "$temp_pass\r"
        }
        "password:" {
            send "$temp_pass\r"
        }
        timeout {
            puts "Connection timed out on $ip"
            continue
        }
    }

    expect {
        -re "(\\$|#|>) $" {
            send "passwd\r"
        }
        "Permission denied" {
            puts "Permission denied for $username@$ip"
            continue
        }
        timeout {
            puts "Login timed out for $username@$ip"
            continue
        }
    }

    expect "New password:"
    send "$new_pass\r"

    expect "Retype new password:"
    send "$new_pass\r"

    expect {
        "password updated successfully" {
            puts "Password changed successfully for $username@$ip"
            send "exit\r"
        }
        "Authentication token manipulation error" {
            puts "Password change failed for $username@$ip"
            send "exit\r"
        }
        timeout {
            puts "Password change timed out for $username@$ip"
        }
    }

    expect eof
}

