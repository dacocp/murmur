package main

import (
	"sort"

	"github.com/mattermost/mattermost/server/public/model"
)

type nativeDMBaselineError struct{ cause error }

func (e *nativeDMBaselineError) Error() string {
	return "native operator DM baseline is not established: " + e.cause.Error()
}

// A verified absence, unlike a timestamp or a missed callback, proves that a
// later native pair has no pre-enable backlog. Completed watch records never
// move this boundary. Existing pairs instead get one quiet canonical adoption.
func nativeOperatorDMWatch(o *Operation) *Operation {
	hash := digest([]string{o.AgentID, o.HumanUserID, o.BotUserID})
	return &Operation{ID: "native-operator-dm-watch-" + hash, Hash: hash,
		Kind: "native_operator_dm_watch", Actor: o.AgentID, AgentID: o.AgentID,
		HumanUserID: o.HumanUserID, BotUserID: o.BotUserID, Status: "complete", Stage: "absent"}
}

// Called under p.mu. This only looks up an existing exact pair; unlike
// GetDirectChannel, GetChannelByName cannot provision a DM. Adoption has no
// native writes, so its grants and canonical observations publish in one save.
func (p *Plugin) discoverNativeOperatorDM(actor, expectedChannel string) (err error) {
	if p.config.OperatorUserID == "" {
		return nil
	}
	a, err := p.requireActor(actor)
	if err != nil {
		return err
	}
	o := &Operation{Kind: "agent_operator_dm", Actor: actor, AgentID: actor,
		HumanUserID: p.config.OperatorUserID, BotUserID: a.UserID}
	wanted := nativeOperatorDMWatch(o)
	watch := p.state.Operations[wanted.ID]
	known := watch != nil && watch.Kind == wanted.Kind && watch.Hash == wanted.Hash && watch.Actor == actor &&
		watch.AgentID == actor && watch.HumanUserID == o.HumanUserID && watch.BotUserID == o.BotUserID &&
		watch.Status == "complete" && watch.Stage == "absent" && watch.ChannelID == ""
	if err := p.validateOperatorDM(o); err != nil {
		if !known && !terminalPolicyError(err) {
			return &nativeDMBaselineError{err}
		}
		return err
	}
	if watch != nil && !known {
		return &nativeDMBaselineError{bad(409, "native operator DM baseline does not match the pinned pair")}
	}
	// Once principals are valid, registration cannot claim success while its
	// first canonical boundary is still unknown. Established watches keep their
	// guarantee through transient refresh failures and recover normally later.
	defer func() {
		if err != nil && !known {
			err = &nativeDMBaselineError{err}
		}
	}()
	// Deleted pairs also disprove absence: restoring one must not turn its old
	// history into new mail. verifyOperatorPair holds them without a baseline.
	native, appErr := p.API.GetChannelByName("", operatorPairName(o), true)
	if appErr != nil {
		if appErr.StatusCode != 404 || expectedChannel != "" {
			return appErr
		}
		// An account/configuration change during the lookup cannot establish a
		// new watch for the old principal. Other failures never prove absence.
		if err := p.validateOperatorDM(o); err != nil {
			return err
		}
		if watch == nil {
			p.state.Operations[wanted.ID] = wanted
			return p.save()
		}
		return nil
	}
	if expectedChannel != "" && (native == nil || native.Id != expectedChannel) {
		return bad(409, "native operator DM hint does not match the canonical pair")
	}
	if err := p.verifyOperatorPair(o, native); err != nil {
		return err
	}
	if p.state.Conversations[native.Id] != nil {
		// Explicit adoption and departures win. Discovery never repairs a
		// missing/observer grant, rejoins an actor, or reclassifies old mail.
		return nil
	}
	o.ChannelID = native.Id
	return p.publishOperatorDM(o, watch == nil)
}

func (p *Plugin) refreshNativeOperatorDM(actor, expectedChannel string) error {
	if p.operatorDMErrors == nil {
		p.operatorDMErrors = map[string]string{}
	}
	err := p.discoverNativeOperatorDM(actor, expectedChannel)
	if err != nil {
		p.operatorDMErrors[actor] = err.Error()
	} else {
		delete(p.operatorDMErrors, actor)
	}
	return err
}

func (p *Plugin) prepareRegisteredOperatorDM(actor string) error {
	err := p.refreshNativeOperatorDM(actor, "")
	if p.fault != "" {
		return bad(503, p.fault)
	}
	if _, held := err.(*nativeDMBaselineError); held {
		return bad(503, err.Error()+"; retry registration")
	}
	return nil
}

func (p *Plugin) discoverNativeOperatorDMs() {
	p.operatorDMErrors = map[string]string{}
	if p.config.OperatorUserID == "" {
		return
	}
	actors := make([]string, 0, len(p.state.Agents))
	for actor := range p.state.Agents {
		actors = append(actors, actor)
	}
	sort.Strings(actors)
	for _, actor := range actors {
		p.refreshNativeOperatorDM(actor, "")
		if p.fault != "" {
			return
		}
	}
}

// Hints only accelerate discovery. Periodic exact-pair probes independently
// recover first posts whose callback was dropped or committed during restart.
func (p *Plugin) discoverHintedOperatorDM(channel string) {
	if p.config.OperatorUserID == "" {
		return
	}
	native, appErr := p.API.GetChannel(channel)
	if appErr != nil || native == nil || native.Id != channel || native.Type != model.ChannelTypeDirect {
		return
	}
	for actor, a := range p.state.Agents {
		o := &Operation{Kind: "agent_operator_dm", Actor: actor, AgentID: actor,
			HumanUserID: p.config.OperatorUserID, BotUserID: a.UserID}
		if native.Name == operatorPairName(o) {
			p.refreshNativeOperatorDM(actor, channel)
			return
		}
	}
}
