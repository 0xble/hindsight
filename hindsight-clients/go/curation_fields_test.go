package hindsight

import (
	"encoding/json"
	"testing"
	"time"
)

func TestCurationFieldsPreservesExplicitNull(t *testing.T) {
	body := NewCurationFields()
	body.SetText("canonical correction")
	omitted, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	var values map[string]interface{}
	if err := json.Unmarshal(omitted, &values); err != nil {
		t.Fatal(err)
	}
	if _, present := values["context"]; present {
		t.Fatal("omitted context was serialized")
	}
	body.SetContextNil()
	body.SetOccurredStartNil()
	encoded, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(encoded, &values); err != nil {
		t.Fatal(err)
	}
	if value, present := values["context"]; !present || value != nil {
		t.Fatal("explicit null context was lost")
	}
	if value, present := values["occurred_start"]; !present || value != nil {
		t.Fatal("explicit null date was lost")
	}
	var decoded CurationFields
	if err := json.Unmarshal(encoded, &decoded); err != nil {
		t.Fatal(err)
	}
	roundtrip, err := json.Marshal(decoded)
	if err != nil {
		t.Fatal(err)
	}
	values = map[string]interface{}{}
	if err := json.Unmarshal(roundtrip, &values); err != nil {
		t.Fatal(err)
	}
	if value, present := values["context"]; !present || value != nil {
		t.Fatal("decoded null context was lost")
	}
	if value, present := values["occurred_start"]; !present || value != nil {
		t.Fatal("decoded null date was lost")
	}
	if _, present := values["occurred_end"]; present {
		t.Fatal("decoded absent date became present")
	}
	decoded.SetContext("replacement context")
	decoded.UnsetOccurredStart()
	updated, err := json.Marshal(decoded)
	if err != nil {
		t.Fatal(err)
	}
	values = map[string]interface{}{}
	if err := json.Unmarshal(updated, &values); err != nil {
		t.Fatal(err)
	}
	if values["context"] != "replacement context" {
		t.Fatal("value setter retained a prior null marker")
	}
	if _, present := values["occurred_start"]; present {
		t.Fatal("unset date was serialized")
	}
}

func TestCurationFieldsSerializesBothDates(t *testing.T) {
	body := NewCurationFields()
	body.SetOccurredStart(time.Date(2024, 1, 1, 0, 0, 0, 0, time.UTC))
	body.SetOccurredEnd(time.Date(2024, 1, 2, 0, 0, 0, 0, time.UTC))
	encoded, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	var values map[string]interface{}
	if err := json.Unmarshal(encoded, &values); err != nil {
		t.Fatal(err)
	}
	if values["occurred_start"] != "2024-01-01T00:00:00Z" || values["occurred_end"] != "2024-01-02T00:00:00Z" {
		t.Fatalf("dates did not serialize as RFC 3339: %s", encoded)
	}
}
