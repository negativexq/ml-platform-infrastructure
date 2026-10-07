package main

import (
	"context"
	"sync"
	"time"

	"golang.org/x/sync/singleflight"
)

// Network/DB work never owns a cache lock. Independent keys have independent flights.
// Shared fills outlive an individual waiter, but are bounded by the provider deadline.
type entityCache[T any] struct {
	mu      sync.RWMutex
	entries map[string]cacheEntry[T]
	flights singleflight.Group
}

func (c *entityCache[T]) lookup(name string, now time.Time) (T, bool) {
	c.mu.RLock()
	entry, ok := c.entries[name]
	c.mu.RUnlock()
	return entry.Value, ok && now.Sub(entry.At) < cacheTTL
}
func (c *entityCache[T]) get(ctx context.Context, name string, now func() time.Time, load func(context.Context) (T, error)) (T, error) {
	var zero T
	if err := ctx.Err(); err != nil {
		return zero, err
	}
	if value, ok := c.lookup(name, now()); ok {
		return value, nil
	}
	pending := c.flights.DoChan(name, func() (any, error) {
		if value, ok := c.lookup(name, now()); ok {
			return value, nil
		}
		op, cancel := context.WithTimeout(context.WithoutCancel(ctx), 6*time.Second)
		defer cancel()
		// Expire relative to the start of the read, not its completion: a slow read
		// must not extend the revocation window of the snapshot it returns.
		started := now()
		value, err := load(op)
		if err != nil {
			return zero, err
		}
		c.mu.Lock()
		if c.entries == nil || len(c.entries) >= 10000 {
			c.entries = map[string]cacheEntry[T]{}
		}
		c.entries[name] = cacheEntry[T]{value, started}
		c.mu.Unlock()
		return value, nil
	})
	select {
	case <-ctx.Done():
		return zero, ctx.Err()
	case result := <-pending:
		if result.Err != nil {
			return zero, result.Err
		}
		return result.Val.(T), nil
	}
}
