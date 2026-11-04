package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"reflect"
	"sort"
	"strings"
)

const metadataSchemaVersion = 1
const metadataBackend = "postgresql-entities-v1"

// The caller holds the plugin mutex. A store commits the complete metadata
// transition, including grants, generation, observations and operation results.
// Native Mattermost writes still have their separate operation journals.
type metadataStore interface {
	Load() (*State, error)
	Save(*State) error
	Close() error
}

func (p *Plugin) persistState(state *State) error {
	if p.fault != "" {
		return fmt.Errorf("persistent metadata is unavailable: %s", p.fault)
	}
	var err error
	if p.metadata == nil {
		err = fmt.Errorf("metadata store was not initialized")
	} else {
		err = p.metadata.Save(state)
	}
	if err != nil {
		// A failed/uncertain commit leaves the in-memory object untrustworthy.
		// Restart reloads SQL; neither production nor tests silently use KV.
		p.fault = err.Error()
	}
	return err
}

type metadataKey struct{ Collection, ID string }
type metadataRecords map[metadataKey]json.RawMessage
type metadataRecord struct {
	Key   metadataKey
	Value json.RawMessage
}
type metadataDelta struct {
	Upsert []metadataRecord
	Delete []metadataKey
}

// Metadata is encoded per top-level map entity. Conversation grants/departures
// have their own rows, so changing one member does not rewrite all its grants.
// JSON field names also cover lineage/enrollment maps without duplicating the
// domain schema here. Strict decoding rejects fields unknown to an older build.
func encodeMetadata(state *State) (metadataRecords, error) {
	if state == nil {
		return nil, fmt.Errorf("cannot persist nil metadata")
	}
	result := metadataRecords{}
	value := reflect.ValueOf(state).Elem()
	for index := 0; index < value.NumField(); index++ {
		field := value.Type().Field(index)
		name := strings.Split(field.Tag.Get("json"), ",")[0]
		if name == "" || name == "-" || strings.HasPrefix(name, "@") {
			return nil, fmt.Errorf("unsupported metadata field %s", field.Name)
		}
		item := value.Field(index)
		if item.Kind() != reflect.Map {
			data, err := json.Marshal(item.Interface())
			if err != nil {
				return nil, err
			}
			result[metadataKey{"@scalar", name}] = data
			continue
		}
		if item.Type().Key().Kind() != reflect.String {
			return nil, fmt.Errorf("metadata map %s requires string keys", name)
		}
		marker := json.RawMessage(`{}`)
		if item.IsNil() {
			marker = json.RawMessage(`null`)
		}
		result[metadataKey{"@map", name}] = marker
		iterator := item.MapRange()
		for iterator.Next() {
			id := iterator.Key().String()
			data, err := json.Marshal(iterator.Value().Interface())
			if err != nil {
				return nil, err
			}
			if name == "conversations" && !bytes.Equal(data, []byte("null")) {
				var object map[string]json.RawMessage
				if err := json.Unmarshal(data, &object); err != nil {
					return nil, err
				}
				for _, nested := range []string{"grants", "left"} {
					var entries map[string]json.RawMessage
					if raw, ok := object[nested]; ok {
						if err := json.Unmarshal(raw, &entries); err != nil {
							return nil, err
						}
						if entries != nil {
							object[nested] = json.RawMessage(`{}`)
						}
					}
					for actor, entry := range entries {
						key, _ := json.Marshal([]string{id, actor})
						result[metadataKey{"@conversation_" + nested, string(key)}] = entry
					}
				}
				data, err = json.Marshal(object)
				if err != nil {
					return nil, err
				}
			}
			result[metadataKey{name, id}] = data
		}
	}
	return result, nil
}

func decodeMetadata(records metadataRecords) (*State, error) {
	object := map[string]json.RawMessage{}
	maps := map[string]map[string]json.RawMessage{}
	fields := reflect.TypeOf(State{})
	for index := 0; index < fields.NumField(); index++ {
		field := fields.Field(index)
		name := strings.Split(field.Tag.Get("json"), ",")[0]
		collection := "@scalar"
		if field.Type.Kind() == reflect.Map {
			collection = "@map"
		}
		data, ok := records[metadataKey{collection, name}]
		if !ok {
			return nil, fmt.Errorf("SQL metadata is missing field %s", name)
		}
		if collection == "@map" {
			var entries map[string]json.RawMessage
			if err := json.Unmarshal(data, &entries); err != nil || len(entries) != 0 {
				return nil, fmt.Errorf("invalid SQL metadata map marker %s", name)
			}
			maps[name] = entries
		} else {
			object[name] = data
		}
	}
	for key, data := range records {
		switch key.Collection {
		case "@scalar":
			if _, ok := object[key.ID]; !ok {
				return nil, fmt.Errorf("SQL metadata has unsupported scalar %s", key.ID)
			}
		case "@map":
			if _, ok := maps[key.ID]; !ok {
				return nil, fmt.Errorf("SQL metadata has unsupported map %s", key.ID)
			}
		case "@conversation_grants", "@conversation_left":
			// Restore these only after the owning conversation rows are present.
		default:
			entries, ok := maps[key.Collection]
			if !ok || entries == nil {
				return nil, fmt.Errorf("SQL metadata has unsupported collection %s", key.Collection)
			}
			entries[key.ID] = data
		}
	}
	for key, data := range records {
		if key.Collection != "@conversation_grants" && key.Collection != "@conversation_left" {
			continue
		}
		var pair []string
		if err := json.Unmarshal([]byte(key.ID), &pair); err != nil || len(pair) != 2 {
			return nil, fmt.Errorf("invalid conversation metadata key")
		}
		var conversation map[string]json.RawMessage
		if err := json.Unmarshal(maps["conversations"][pair[0]], &conversation); err != nil || conversation == nil {
			return nil, fmt.Errorf("conversation metadata has no owner %s", pair[0])
		}
		name := strings.TrimPrefix(key.Collection, "@conversation_")
		entries := map[string]json.RawMessage{}
		if raw := conversation[name]; raw != nil {
			if err := json.Unmarshal(raw, &entries); err != nil || entries == nil {
				return nil, fmt.Errorf("invalid conversation %s metadata", name)
			}
		}
		entries[pair[1]] = data
		conversation[name], _ = json.Marshal(entries)
		maps["conversations"][pair[0]], _ = json.Marshal(conversation)
	}
	for name, entries := range maps {
		object[name], _ = json.Marshal(entries)
	}
	data, err := json.Marshal(object)
	if err != nil {
		return nil, err
	}
	return decodeState(data)
}

func decodeState(data []byte) (*State, error) {
	if trimmed := bytes.TrimSpace(data); len(trimmed) == 0 || trimmed[0] != '{' {
		return nil, fmt.Errorf("metadata must be a JSON object")
	}
	var state State
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&state); err != nil {
		return nil, fmt.Errorf("incompatible metadata: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return nil, fmt.Errorf("metadata has trailing JSON")
	}
	state.init()
	return &state, nil
}

func compareMetadata(before, after metadataRecords) metadataDelta {
	var delta metadataDelta
	for key, data := range after {
		if !bytes.Equal(before[key], data) {
			delta.Upsert = append(delta.Upsert, metadataRecord{key, data})
		}
	}
	for key := range before {
		if _, ok := after[key]; !ok {
			delta.Delete = append(delta.Delete, key)
		}
	}
	less := func(a, b metadataKey) bool {
		return a.Collection < b.Collection || a.Collection == b.Collection && a.ID < b.ID
	}
	sort.Slice(delta.Upsert, func(i, j int) bool { return less(delta.Upsert[i].Key, delta.Upsert[j].Key) })
	sort.Slice(delta.Delete, func(i, j int) bool { return less(delta.Delete[i], delta.Delete[j]) })
	return delta
}
