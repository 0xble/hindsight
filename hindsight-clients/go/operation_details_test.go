package hindsight

import (
	"encoding/json"
	"testing"
)

func TestOperationResponseDetailsRejectInvalidDiscriminator(t *testing.T) {
	payloads := []string{
		`{"operation_type":"unknown","outcome":"content_written"}`,
		`{"outcome":"content_written"}`,
		`{"operation_type":null,"outcome":"content_written"}`,
		`{"operation_type":"file_convert_retain","outcome":"content_written"}`,
		`{"operation_type":"refresh_mental_model","failure_class":"low_quality_ocr","failure_reason":"no_meaningful_text"}`,
		`{}`,
		`""`,
	}
	for _, payload := range payloads {
		t.Run(payload, func(t *testing.T) {
			var details OperationResponseDetails
			if err := json.Unmarshal([]byte(payload), &details); err == nil {
				t.Fatal("accepted unknown/missing discriminator or mismatched shape")
			}
			var status OperationStatusResponse
			if err := json.Unmarshal([]byte(`{"operation_id":"op","status":"failed","details":`+payload+`}`), &status); err == nil {
				t.Fatal("parent accepted invalid details")
			}
		})
	}
}

func TestOperationResponseDetailsNull(t *testing.T) {
	details := FileConvertRetainOperationDetailsAsOperationResponseDetails(NewFileConvertRetainOperationDetails("low_quality_ocr", "no_meaningful_text"))
	if err := json.Unmarshal([]byte(" \n null "), &details); err != nil {
		t.Fatal(err)
	}
	if details.FileConvertRetainOperationDetails != nil || details.RefreshMentalModelOperationDetails != nil {
		t.Fatal("null must clear both variants")
	}
	data, err := json.Marshal(details)
	if err != nil || string(data) != "null" {
		t.Fatalf("null roundtrip: %s, %v", data, err)
	}
	var nullable NullableOperationResponseDetails
	if err := json.Unmarshal([]byte("null"), &nullable); err != nil || !nullable.IsSet() || nullable.Get() != nil {
		t.Fatalf("nullable wrapper: %#v, %v", nullable, err)
	}
	for _, field := range []string{"", `,"details":null`} {
		var status OperationStatusResponse
		if err := json.Unmarshal([]byte(`{"operation_id":"op","status":"completed"`+field+`}`), &status); err != nil {
			t.Fatal(err)
		}
		if status.Details.Get() != nil || status.Details.IsSet() != (field != "") {
			t.Fatal("parent nullable semantics changed")
		}
		var response OperationResponse
		if err := json.Unmarshal([]byte(`{"id":"op","task_type":"file_convert_retain","items_count":1,"created_at":"2026-09-01T00:00:00Z","status":"completed","error_message":null`+field+`}`), &response); err != nil {
			t.Fatal(err)
		}
		if response.Details.Get() != nil || response.Details.IsSet() != (field != "") {
			t.Fatal("list parent nullable semantics changed")
		}
		for _, parent := range []interface{}{status, response} {
			data, err := json.Marshal(parent)
			if err != nil {
				t.Fatal(err)
			}
			var wire map[string]json.RawMessage
			if err := json.Unmarshal(data, &wire); err != nil {
				t.Fatal(err)
			}
			detail, present := wire["details"]
			if present != (field != "") || (present && string(detail) != "null") {
				t.Fatalf("parent null/absent roundtrip changed: %s", data)
			}
		}
	}
}

func TestOperationResponseDetailsUseDiscriminator(t *testing.T) {
	tests := []struct {
		name          string
		payload       string
		fileDetail    bool
		refreshDetail bool
	}{
		{
			name:       "file conversion failure",
			payload:    `{"operation_type":"file_convert_retain","failure_class":"low_quality_ocr","failure_reason":"no_meaningful_text"}`,
			fileDetail: true,
		},
		{
			name:       "no extractable text",
			payload:    `{"operation_type":"file_convert_retain","failure_class":"no_extractable_text","failure_reason":"empty_content","parsers":["markitdown"]}`,
			fileDetail: true,
		},
		{
			name:          "mental model refresh",
			payload:       `{"operation_type":"refresh_mental_model","outcome":"content_written","failure_reason":null}`,
			refreshDetail: true,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			var details OperationResponseDetails
			if err := json.Unmarshal([]byte(test.payload), &details); err != nil {
				t.Fatalf("unmarshal operation details: %v", err)
			}
			if got := details.FileConvertRetainOperationDetails != nil; got != test.fileDetail {
				t.Fatalf("file-conversion detail present = %v, want %v", got, test.fileDetail)
			}
			if got := details.RefreshMentalModelOperationDetails != nil; got != test.refreshDetail {
				t.Fatalf("refresh detail present = %v, want %v", got, test.refreshDetail)
			}
		})
	}
}
