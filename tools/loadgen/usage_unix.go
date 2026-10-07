//go:build darwin || linux

package main

import (
	"runtime"
	"syscall"
)

func processUsage() (float64, int64) {
	var r syscall.Rusage
	if syscall.Getrusage(syscall.RUSAGE_SELF, &r) != nil {
		return 0, 0
	}
	cpu := float64(r.Utime.Sec+r.Stime.Sec) + float64(r.Utime.Usec+r.Stime.Usec)/1e6
	rss := r.Maxrss
	if runtime.GOOS == "linux" {
		rss *= 1024
	}
	return cpu, rss
}
