package main

import "time"

const canonicalRepairInterval = 5 * time.Second

// Called under p.mu. Observations and grants stay in one durable generation;
// multiple actor polls share one canonical repair instead of rereading all
// histories. Explicit membership/adoption snapshots continue to call canonical
// directly. Restart always forces a complete repair; no timestamp watermark is
// used to assume that missed native callbacks cannot exist.
func (p *Plugin) repair(force bool) {
	if p.fault != "" {
		return
	}
	p.drainPostHints()
	if p.fault != "" {
		return
	}
	clock := p.repairClock
	if clock == nil {
		clock = time.Now
	}
	now := clock()
	if !force && !p.nextRepair.IsZero() && now.Before(p.nextRepair) {
		return
	}
	// Failed canonical reads are retried on the next interval. Their lastErr
	// remains visible between attempts; persistence failures remain p.fault.
	p.reconcile()
	// Measure from completion: a slow sweep must not make every already-queued
	// actor request immediately repeat the same full scan.
	p.nextRepair = clock().Add(canonicalRepairInterval)
}
