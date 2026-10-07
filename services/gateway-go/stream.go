package main

import (
	"context"
	"io"
	"sync"
	"sync/atomic"
	"time"
)

type operationWatch struct {
	mu       sync.Mutex
	timer    *time.Timer
	duration time.Duration
	deadline time.Time
	expired  atomic.Bool
	stopped  bool
}

func newOperationWatch(d time.Duration, cancel context.CancelFunc) *operationWatch {
	w := &operationWatch{duration: d, deadline: time.Now().Add(d)}
	w.timer = time.AfterFunc(d, func() {
		w.mu.Lock()
		defer w.mu.Unlock()
		if !w.stopped {
			if remaining := time.Until(w.deadline); remaining > 0 {
				w.timer.Reset(remaining)
				return
			}
			w.expired.Store(true)
			cancel()
		}
	})
	return w
}
func (w *operationWatch) touch() {
	w.mu.Lock()
	defer w.mu.Unlock()
	if !w.stopped && !w.expired.Load() {
		w.deadline = time.Now().Add(w.duration)
		w.timer.Reset(w.duration)
	}
}
func (w *operationWatch) stop() { w.mu.Lock(); defer w.mu.Unlock(); w.stopped = true; w.timer.Stop() }
func (b *closingBody) Read(p []byte) (int, error) {
	b.watch.touch()
	n, e := b.ReadCloser.Read(p)
	if n > 0 {
		b.watch.touch()
	}
	return n, e
}

type meteredReader struct {
	reader io.Reader
	meter  *tokenMeter
}

func (m *meteredReader) Read(p []byte) (int, error) {
	n, e := m.reader.Read(p)
	m.meter.feed(p[:n])
	return n, e
}
