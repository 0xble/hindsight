package hindsight

import (
	"encoding/json"
	"testing"
)

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
