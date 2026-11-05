package main

import (
	"encoding/base64"
	"encoding/json"
	"net/http"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/mattermost/mattermost/server/public/model"
)

const managedReadVersion = 1

func (p *Plugin) managedReadCapability() map[string]any {
	return map[string]any{"version": managedReadVersion, "enabled": p.config.AllowManagedConversationReads}
}

func requestedReadVersion(r *http.Request) (int, error) {
	values, present := r.URL.Query()["read_version"]
	if !present {
		return 0, nil
	}
	if len(values) != 1 || values[0] != "1" {
		return 0, bad(409, "read_version=1 is required; update the Murmur client")
	}
	return managedReadVersion, nil
}

// Content visibility is independent of membership and posting. In particular,
// membership() deliberately keeps using access() and cannot inherit this policy.
func (p *Plugin) contentRead(actor, channel string, version int) (*Conversation, error) {
	c, err := p.access(actor, channel, false)
	if err == nil {
		return c, nil
	}
	if failure, ok := err.(*httpError); !ok || failure.status != 403 {
		return nil, err
	}
	if _, actorErr := p.requireActor(actor); actorErr != nil {
		return nil, actorErr
	}
	if !p.config.AllowManagedConversationReads {
		return nil, err
	}
	c = p.state.Conversations[channel]
	if c == nil {
		return nil, bad(404, "conversation not found")
	}
	if version != managedReadVersion {
		return nil, bad(409, "shared conversation reads require read_version=1; update the Murmur client")
	}
	return c, nil
}

type managedParticipant struct {
	Kind     string `json:"kind"`
	ID       string `json:"id"`
	Name     string `json:"name"`
	Username string `json:"username"`
}

// DMs retain their fixed native pair even after a grant is relinquished. Channel
// participants are the supported Murmur full-member grants, not observer reads
// or an import of arbitrary native membership changes.
func (p *Plugin) managedParticipants(c *Conversation) ([]managedParticipant, string, error) {
	participants := []managedParticipant{}
	name := c.Name
	if c.Kind == "dm" {
		currentName, pair, err := p.operatorDMIdentity(c.ID)
		if err != nil {
			return nil, "", err
		}
		name = currentName
		for _, userID := range pair {
			var agent *Agent
			for _, a := range p.state.Agents {
				if a.UserID == userID {
					agent = a
					break
				}
			}
			if agent != nil {
				participants = append(participants, managedParticipant{"agent", agent.ID, agent.Name, agent.Username})
			} else {
				user, err := p.API.GetUser(userID)
				if err != nil {
					return nil, "", err
				}
				if user == nil || user.Id != userID {
					return nil, "", bad(503, "canonical DM participant identity mismatch")
				}
				participants = append(participants, managedParticipant{"human", user.Id, user.Username, user.Username})
			}
		}
	} else {
		for actor, grant := range c.Grants {
			if grant.Role != "member" {
				continue
			}
			a, err := p.requireActor(actor)
			if err != nil {
				return nil, "", bad(503, "managed participant is not enrolled")
			}
			participants = append(participants, managedParticipant{"agent", a.ID, a.Name, a.Username})
		}
	}
	sort.Slice(participants, func(i, j int) bool { return participants[i].ID < participants[j].ID })
	return participants, name, nil
}

type managedDirectoryCursor struct {
	After string `json:"after"`
	Scope string `json:"scope"`
}

func (p *Plugin) managedConversationDirectory(actor string, r *http.Request) (map[string]any, error) {
	if _, err := p.requireActor(actor); err != nil {
		return nil, err
	}
	if !p.config.AllowManagedConversationReads {
		return nil, bad(403, "shared managed conversation reads are disabled")
	}
	version, err := requestedReadVersion(r)
	if err != nil {
		return nil, err
	}
	if version != managedReadVersion {
		return nil, bad(409, "conversation discovery requires read_version=1; update the Murmur client")
	}
	limit, err := queryNumber(r, "limit", 20, 1, 100)
	if err != nil {
		return nil, err
	}
	query := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("query")))
	if !utf8.ValidString(query) || utf8.RuneCountInString(query) > 256 {
		return nil, bad(400, "query accepts at most 256 characters")
	}
	kind, agent := r.URL.Query().Get("kind"), r.URL.Query().Get("agent")
	if kind != "" && kind != "dm" && kind != "channel" {
		return nil, bad(400, "kind must be dm or channel")
	}
	if agent != "" {
		if _, err := p.requireActor(agent); err != nil {
			return nil, bad(400, "agent filter must identify an enrolled agent")
		}
	}
	scope := digest(struct{ Actor, Query, Kind, Agent string }{actor, query, kind, agent})
	after := ""
	if raw := r.URL.Query().Get("after"); raw != "" {
		if len(raw) > 512 {
			return nil, bad(400, "invalid conversation directory cursor")
		}
		data, err := base64.RawURLEncoding.DecodeString(raw)
		var cursor managedDirectoryCursor
		if err != nil || json.Unmarshal(data, &cursor) != nil || !model.IsValidId(cursor.After) || cursor.Scope != scope {
			return nil, bad(400, "conversation cursor does not match this actor and query; restart discovery")
		}
		after = cursor.After
	}
	ids := make([]string, 0, len(p.state.Conversations))
	for id := range p.state.Conversations {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	items := []map[string]any{}
	foundAfter := after == ""
	var next any
	for _, id := range ids {
		c := p.state.Conversations[id]
		if kind != "" && conversationKind(c) != kind {
			continue
		}
		participants, name, err := p.managedParticipants(c)
		if err != nil {
			return nil, err
		}
		participantMatch, participantText := agent == "", ""
		for _, participant := range participants {
			participantMatch = participantMatch || participant.Kind == "agent" && participant.ID == agent
			participantText += " " + participant.Name + " " + participant.Username + " " + participant.ID
		}
		if !participantMatch || !strings.Contains(strings.ToLower(id+" "+name+" "+c.Purpose+participantText), query) {
			continue
		}
		if !foundAfter {
			if id == after {
				foundAfter = true
			}
			continue
		}
		if len(items) == limit {
			cursor, _ := json.Marshal(managedDirectoryCursor{items[len(items)-1]["id"].(string), scope})
			next = base64.RawURLEncoding.EncodeToString(cursor)
			break
		}
		item := view(c, actor)
		item["name"], item["participants"] = name, participants
		items = append(items, item)
	}
	if !foundAfter {
		return nil, bad(400, "conversation cursor no longer matches this query; restart discovery")
	}
	return map[string]any{"conversations": items, "next_after": next}, nil
}
