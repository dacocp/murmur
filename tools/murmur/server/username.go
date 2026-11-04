package main

import (
	"strings"

	"github.com/mattermost/mattermost/server/public/model"
)

// A bot can commit before its create response arrives. Freeze its initial name
// first, then persist its verified ID before renaming or adding membership.
type RegistrationIntent struct {
	Mode            string `json:"mode,omitempty"`
	Parent          string `json:"parent,omitempty"`
	Username        string `json:"username"`
	UserID          string `json:"user_id,omitempty"`
	CreateAttempted bool   `json:"create_attempted,omitempty"`
}

func readableUsername(actor, label string) string {
	var slug strings.Builder
	separator := false
	for _, ch := range strings.ToLower(label) {
		if ch >= 'a' && ch <= 'z' || ch >= '0' && ch <= '9' {
			if separator && slug.Len() > 0 {
				slug.WriteByte('-')
			}
			slug.WriteRune(ch)
			separator = false
		} else {
			separator = true
		}
	}
	base := slug.String()
	if base == "" {
		base = "agent"
	} else if base[0] >= '0' && base[0] <= '9' {
		base = "agent-" + base
	}
	// Compact enough for a sidebar, below the pinned model's 64-byte maximum.
	if len(base) > 23 {
		base = strings.TrimRight(base[:23], "-")
	}
	return base + "-" + digest(actor)[:8]
}

func actorDescription(actor string) string { return "Murmur session: " + actor }

func (p *Plugin) ownedBot(actor, userID string, markerRequired bool) (*model.Bot, error) {
	user, appErr := p.API.GetUser(userID)
	if appErr != nil {
		return nil, appErr
	}
	if user == nil || user.Id != userID || !user.IsBot || user.DeleteAt != 0 {
		return nil, bad(409, "native account identity collision")
	}
	bot, err := p.API.GetBot(userID, false)
	if err != nil {
		return nil, err
	}
	if bot == nil || bot.UserId != userID || bot.OwnerId != "murmur" || bot.DeleteAt != 0 || bot.Username != user.Username ||
		(markerRequired && bot.Description != actorDescription(actor)) {
		return nil, bad(409, "native account identity collision")
	}
	return bot, nil
}

func (p *Plugin) syncBotIdentity(actor, userID, name string, prefix *string, markerRequired bool) (*model.Bot, error) {
	bot, err := p.ownedBot(actor, userID, markerRequired)
	if err != nil {
		return nil, err
	}
	patch := &model.BotPatch{}
	if bot.DisplayName != name {
		patch.DisplayName = &name
	}
	if prefix != nil {
		username := readableUsername(actor, *prefix)
		if username != bot.Username {
			// Never let an explicit handle change select or adopt another account.
			other, lookupErr := p.API.GetUserByUsername(username)
			if lookupErr == nil && (other == nil || other.Id != userID) {
				return nil, bad(409, "requested username is already in use")
			}
			if lookupErr != nil && lookupErr.StatusCode != 404 {
				return nil, lookupErr
			}
			patch.Username = &username
		}
	}
	if patch.Username != nil || patch.DisplayName != nil {
		if _, err := p.API.PatchBot(userID, patch); err != nil {
			return nil, err
		}
		// Return the canonical username, including recovery from a prior lost
		// patch response. Existing enrolled legacy bots need no new marker.
		return p.ownedBot(actor, userID, markerRequired)
	}
	return bot, nil
}

func (p *Plugin) registrationBot(actor, name, parent string, prefix *string, mode string) (*RegistrationIntent, error) {
	intent := p.state.Registrations[actor]
	if intent != nil && intent.Parent != parent {
		return nil, bad(409, "pending registration cannot be reparented")
	}
	if intent != nil && intent.Mode != "" && intent.Mode != mode {
		return nil, bad(409, "pending registration cannot change enrollment mode")
	}
	if intent == nil {
		label := name
		if prefix != nil {
			label = *prefix
		}
		intent = &RegistrationIntent{Parent: parent, Mode: mode, Username: readableUsername(actor, label)}
		p.state.Registrations[actor] = intent
		if err := p.save(); err != nil {
			return nil, err
		}
	}
	if intent.Mode == "" {
		intent.Mode = mode
		if err := p.save(); err != nil {
			return nil, err
		}
	}
	if intent.UserID == "" {
		user, lookupErr := p.API.GetUserByUsername(intent.Username)
		if lookupErr != nil {
			if lookupErr.StatusCode != 404 {
				return nil, lookupErr
			}
			intent.CreateAttempted = true
			if err := p.save(); err != nil {
				return nil, err
			}
			bot, err := p.API.CreateBot(&model.Bot{Username: intent.Username, DisplayName: name,
				Description: actorDescription(actor), OwnerId: "murmur"})
			if err != nil {
				return nil, err
			}
			if bot == nil {
				return nil, bad(409, "native account creation returned no bot")
			}
			user = &model.User{Id: bot.UserId}
		}
		if user == nil {
			return nil, bad(409, "native account lookup returned no user")
		}
		if _, err := p.ownedBot(actor, user.Id, true); err != nil {
			if collision, ok := err.(*httpError); ok && collision.status == 409 && !intent.CreateAttempted {
				// Nothing was sent to CreateBot for this intent. A proven foreign
				// occupant can safely release the name for a different-prefix retry.
				// An uncertain creation must retain its exact original binding.
				delete(p.state.Registrations, actor)
				if saveErr := p.save(); saveErr != nil {
					return nil, saveErr
				}
			}
			return nil, err
		}
		intent.UserID = user.Id
		if err := p.save(); err != nil {
			return nil, err
		}
	}
	return intent, nil
}
