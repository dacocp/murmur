package main

import (
	"sort"
	"strings"

	"github.com/mattermost/mattermost/server/public/model"
)

type operatorDMRequest struct {
	OpID    string `json:"op_id"`
	AgentID string `json:"agent_id"`
}

func (p *Plugin) operatorUser(id string) (*model.User, error) {
	user, appErr := p.API.GetUser(id)
	if appErr != nil {
		if appErr.StatusCode == 403 || appErr.StatusCode == 404 {
			return nil, bad(403, "system administrator access required")
		}
		return nil, appErr
	}
	if user == nil || user.Id != id || user.DeleteAt != 0 || user.IsBot || !user.IsSystemAdmin() || !p.API.HasPermissionTo(id, model.PermissionManageSystem) {
		return nil, bad(403, "system administrator access required")
	}
	return user, nil
}

func (p *Plugin) operatorDM(userID string, req operatorDMRequest) (map[string]any, error) {
	if req.OpID == "" || len(req.OpID) > 200 {
		return nil, bad(400, "op_id is required (maximum 200 characters)")
	}
	o := p.state.Operations[req.OpID]
	if o != nil {
		if o.Kind != "operator_dm" || o.Actor != userID || o.Hash != digest(req) {
			return nil, bad(409, "operation ID is already bound to another immutable request")
		}
	} else {
		a, err := p.requireActor(req.AgentID)
		if err != nil {
			return nil, err
		}
		o = &Operation{ID: req.OpID, Kind: "operator_dm", Actor: userID, Hash: digest(req),
			AgentID: req.AgentID, BotUserID: a.UserID, Status: "pending", Stage: "intent"}
		if err := p.startOperatorDM(o); err != nil {
			return nil, err
		}
	}
	if err := p.finishOperatorDM(o); err != nil {
		return nil, err
	}
	if o.Status != "complete" {
		return nil, bad(409, o.Error)
	}
	// Completed retries are historical results, even if the target later left
	// or disappeared. They do not check/repair grants or resolve another pair.
	return map[string]any{"conversation": map[string]string{"id": o.ChannelID, "name": o.Name, "kind": "dm"}}, nil
}

// Agent initiation owns its own operation. The human is a configured recipient,
// never an impersonated caller and never an enrolled native actor.
func (p *Plugin) agentOperatorDM(req memberRequest) (map[string]any, error) {
	a, err := p.requireActor(req.Actor)
	if err != nil {
		return nil, err
	}
	if req.OpID == "" || len(req.OpID) > 200 {
		return nil, bad(400, "op_id is required (maximum 200 characters)")
	}
	o := p.state.Operations[req.OpID]
	if o != nil {
		if o.Kind != "agent_operator_dm" || o.Actor != req.Actor || o.Hash != digest(req) {
			return nil, bad(409, "operation ID is already bound to another immutable request")
		}
	} else {
		o = &Operation{ID: req.OpID, Kind: "agent_operator_dm", Actor: req.Actor, Hash: digest(req),
			AgentID: req.Actor, BotUserID: a.UserID, HumanUserID: p.config.OperatorUserID, Status: "pending", Stage: "intent"}
		if err := p.startOperatorDM(o); err != nil {
			return nil, err
		}
	}
	if err := p.finishOperatorDM(o); err != nil {
		return nil, err
	}
	if o.Status != "complete" {
		return nil, bad(409, o.Error)
	}
	// Ownership permits the historical result after departure/config changes.
	// Returning it neither repairs access nor resolves another native pair.
	return map[string]any{"conversation": map[string]string{"id": o.ChannelID, "name": o.Name, "kind": "dm"}}, nil
}

func (p *Plugin) startOperatorDM(o *Operation) error {
	if err := p.validateOperatorDM(o); err != nil {
		return err
	}
	if err := p.settleOperatorPair(o); err != nil {
		return err
	}
	p.state.Operations[o.ID] = o
	return p.save()
}

func operatorHumanID(o *Operation) string {
	if o.Kind == "agent_operator_dm" {
		return o.HumanUserID
	}
	return o.Actor // Existing human-owned journal format remains unchanged.
}

func (p *Plugin) validateOperatorDM(o *Operation) error {
	human := operatorHumanID(o)
	if o.Kind == "agent_operator_dm" {
		if o.Actor != o.AgentID {
			return bad(409, "agent DM must use its initiating actor's own bot")
		}
		if p.config.OperatorUserID == "" {
			return bad(403, "agent-initiated operator DMs are disabled; configure Operator user ID")
		}
		if human != p.config.OperatorUserID {
			return bad(409, "operator configuration changed; the pending DM cannot be rerouted")
		}
	}
	if !model.IsValidId(human) {
		return bad(409, "operator user ID is invalid")
	}
	if _, err := p.operatorUser(human); err != nil {
		return err
	}
	a := p.state.Agents[o.AgentID]
	if a == nil || a.UserID != o.BotUserID || !model.IsValidId(o.BotUserID) || o.BotUserID == human {
		return bad(409, "registered agent no longer matches the pinned DM bot")
	}
	bot, appErr := p.API.GetBot(o.BotUserID, false)
	if appErr != nil {
		return appErr
	}
	if bot == nil || bot.UserId != o.BotUserID || bot.OwnerId != "murmur" || bot.DeleteAt != 0 {
		return bad(409, "DM target is not an active Murmur-owned bot")
	}
	user, appErr := p.API.GetUser(o.BotUserID)
	if appErr != nil {
		return appErr
	}
	if user == nil || user.Id != o.BotUserID || !user.IsBot || user.DeleteAt != 0 {
		return bad(409, "DM target bot account is unavailable")
	}
	return nil
}

func operatorPairName(o *Operation) string {
	ids := []string{operatorHumanID(o), o.BotUserID}
	sort.Strings(ids)
	return strings.Join(ids, "__")
}

func (p *Plugin) verifyOperatorPair(o *Operation, native *model.Channel) error {
	if native == nil || !model.IsValidId(native.Id) || (o.ChannelID != "" && native.Id != o.ChannelID) || native.Type != model.ChannelTypeDirect || native.Name != operatorPairName(o) || native.DeleteAt != 0 {
		return bad(409, "native channel does not match the pinned human/bot pair")
	}
	members, appErr := p.API.GetChannelMembers(native.Id, 0, 3)
	if appErr != nil {
		return appErr
	}
	if len(members) != 2 || members[0].UserId == members[1].UserId {
		return bad(409, "native DM membership is inconsistent")
	}
	for _, member := range members {
		if member.ChannelId != native.Id || (member.UserId != operatorHumanID(o) && member.UserId != o.BotUserID) {
			return bad(409, "native DM membership does not match the pinned pair")
		}
	}
	return nil
}

// Policy conflicts retire only this intent. A revoked principal must not turn
// ordinary agent feeds into a permanent global reconciliation failure.
func (p *Plugin) finishOperatorDM(o *Operation) error {
	if o.Status == "complete" || o.Status == "failed" {
		return nil
	}
	err := p.publishOperatorDM(o)
	if err == nil || p.fault != "" {
		return err
	}
	terminal := false
	switch failure := err.(type) {
	case *httpError:
		terminal = failure.status == 403 || failure.status == 409
	case *model.AppError:
		terminal = failure.StatusCode == 403 || failure.StatusCode == 404 || failure.StatusCode == 410
	}
	if terminal {
		o.Status, o.Error = "failed", err.Error()
		return p.save()
	}
	return err
}

func (p *Plugin) publishOperatorDM(o *Operation) error {
	if err := p.validateOperatorDM(o); err != nil {
		return err
	}
	var native *model.Channel
	var appErr *model.AppError
	if o.ChannelID == "" {
		native, appErr = p.API.GetDirectChannel(operatorHumanID(o), o.BotUserID)
	} else {
		native, appErr = p.API.GetChannel(o.ChannelID)
	}
	if appErr != nil {
		return appErr
	}
	if err := p.verifyOperatorPair(o, native); err != nil {
		return err
	}
	if o.ChannelID == "" {
		o.ChannelID, o.Stage = native.Id, "pair"
		if err := p.save(); err != nil {
			return err
		}
	}
	name, err := p.operatorDMName(o.ChannelID)
	if err != nil {
		return err
	}
	c := p.state.Conversations[o.ChannelID]
	if c != nil {
		if c.Kind != "dm" || c.Left[o.AgentID] || c.Grants[o.AgentID].Role != "member" {
			return bad(409, "managed DM access is inconsistent or the agent left; opening cannot repair or rejoin it")
		}
	}
	var baseline []*model.Post
	if c == nil {
		baseline, err = p.canonical(o.ChannelID)
		if err != nil {
			return err
		}
		for _, post := range baseline {
			if post.ChannelId != o.ChannelID {
				return bad(503, "canonical DM snapshot channel mismatch")
			}
		}
	}
	// External account/membership changes need a fresh check after snapshot work.
	if err := p.validateOperatorDM(o); err != nil {
		return err
	}
	native, appErr = p.API.GetChannel(o.ChannelID)
	if appErr != nil {
		return appErr
	}
	if err := p.verifyOperatorPair(o, native); err != nil {
		return err
	}
	if c == nil {
		c = &Conversation{ID: o.ChannelID, Name: name, Kind: "dm", Grants: map[string]Grant{}}
		p.state.Conversations[c.ID] = c
		p.observe(baseline)
		for _, post := range baseline {
			if userPost(post) {
				observation := p.state.Observed[post.Id]
				observation.Historical = true
				p.state.Observed[post.Id] = observation
			}
		}
		p.grant(c, o.AgentID, "member")
	}
	o.Name, o.Status, o.Error = name, "complete", ""
	// First managed visibility, exact baseline, all inherited grants/generations
	// and completion have one durable publication. No managed stub is saved.
	return p.save()
}

// Fresh opens cannot overtake accepted conflicting pair or membership intents.
// This runs before allocating the new journal, so recovery cannot find itself.
func (p *Plugin) settleOperatorPair(proposed *Operation) error {
	for _, earlier := range p.state.Operations {
		if earlier.Status == "complete" || earlier.Status == "failed" {
			continue
		}
		var err error
		switch earlier.Kind {
		case "operator_dm", "agent_operator_dm":
			if operatorHumanID(earlier) != operatorHumanID(proposed) || earlier.BotUserID != proposed.BotUserID {
				continue
			}
			err = p.finishOperation(earlier)
		case "join", "leave":
			if earlier.Actor != proposed.AgentID {
				continue
			}
			native, appErr := p.API.GetChannel(earlier.ChannelID)
			if appErr != nil {
				return appErr
			}
			if native.Type != model.ChannelTypeDirect || native.Name != operatorPairName(proposed) {
				continue
			}
			err = p.settleMembership(proposed.AgentID, earlier.ChannelID)
		default:
			continue
		}
		if (err != nil && earlier.Status != "failed") || (earlier.Status != "complete" && earlier.Status != "failed") {
			return bad(409, "earlier DM or membership operation is pending; retry after reconciliation")
		}
	}
	return nil
}
