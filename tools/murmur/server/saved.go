package main

import (
	"sort"

	"github.com/mattermost/mattermost/server/public/model"
)

type savedRequest struct {
	Actor   string `json:"actor"`
	Starred *bool  `json:"starred"`
}

// Mattermost preferences are the only bookmark authority. Fetch by user rather
// than GetFlaggedPosts: native membership filtering excludes Murmur observers.
func (p *Plugin) saved(actor string) (map[string]any, error) {
	a, err := p.requireActor(actor)
	if err != nil {
		return nil, err
	}
	preferences, appErr := p.API.GetPreferencesForUser(a.UserID)
	if appErr != nil {
		return nil, appErr
	}
	ids := []string{}
	for _, pref := range preferences {
		if pref.UserId == a.UserID && pref.Category == model.PreferenceCategoryFlaggedPost && pref.Value == "true" {
			ids = append(ids, pref.Name)
		}
	}
	sort.Strings(ids)
	saved := []map[string]any{}
	for _, id := range ids {
		post, err := p.authorizedPost(actor, id)
		if err != nil {
			if failure, ok := err.(*httpError); ok && (failure.status == 403 || failure.status == 404) {
				saved = append(saved, map[string]any{"post_id": id, "unavailable": true})
				continue
			}
			return nil, err
		}
		saved = append(saved, map[string]any{"post_id": id, "post": p.postView(post),
			"conversation": view(p.state.Conversations[post.ChannelId], actor)})
	}
	return map[string]any{"saved": saved}, nil
}

func (p *Plugin) setSaved(id string, req savedRequest) (map[string]any, error) {
	a, err := p.requireActor(req.Actor)
	if err != nil {
		return nil, err
	}
	if !model.IsValidId(id) {
		return nil, bad(400, "invalid Mattermost post ID")
	}
	if req.Starred == nil {
		return nil, bad(400, "starred boolean is required")
	}
	preference := model.Preference{UserId: a.UserID, Category: model.PreferenceCategoryFlaggedPost, Name: id, Value: "true"}
	if *req.Starred {
		post, err := p.authorizedPost(req.Actor, id)
		if err != nil {
			return nil, err
		}
		if post.Type != "" {
			return nil, bad(400, "only ordinary user messages can be saved")
		}
		if err := p.API.UpdatePreferencesForUser(a.UserID, []model.Preference{preference}); err != nil {
			return nil, err
		}
	} else {
		if err := p.API.DeletePreferencesForUser(a.UserID, []model.Preference{preference}); err != nil {
			return nil, err
		}
	}
	return map[string]any{"post_id": id, "starred": *req.Starred}, nil
}
