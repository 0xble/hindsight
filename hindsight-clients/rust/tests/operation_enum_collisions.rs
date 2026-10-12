use hindsight_client::types;
use serde_json::json;

#[test]
fn operation_and_curation_statuses_keep_their_own_wire_values() {
    for status in [
        "pending",
        "processing",
        "completed",
        "failed",
        "cancelled",
        "not_found",
    ] {
        let response: types::OperationStatusResponse = serde_json::from_value(json!({
            "operation_id": "operation-1", "status": status
        }))
        .expect("operation statuses must not alias curation receipt statuses");
        assert_eq!(serde_json::to_value(response).unwrap()["status"], status);
    }
    for status in ["applied", "reverted"] {
        let receipt: types::CurationReceipt = serde_json::from_value(json!({
            "bank_id": "bank-1", "batch_id": "batch-1",
            "manifest_revision": "a".repeat(64), "receipt_revision": "b".repeat(64),
            "status": status,
            "inventory": {"targets": 1, "observations": 0, "peers": 0, "entities": 0,
                "links": 0, "history_rows": 0, "snapshot_bytes": 1, "source_bytes": 0},
            "maintenance_debt": {"consolidation": true, "graph": true, "model_refresh": true, "memory_ids": []}
        })).expect("curation statuses must not alias operation statuses");
        assert_eq!(serde_json::to_value(receipt).unwrap()["status"], status);
    }
}

#[test]
fn refresh_and_ocr_failure_reasons_do_not_alias() {
    for reason in [
        "empty_candidate",
        "structured_doc_unreadable",
        "delta_ops_failed",
        "delta_ops_all_skipped",
        "delta_not_applied",
        "structured_output_failed",
        "retrieval_failed",
        "no_answer",
        "unexpected_error",
    ] {
        let details: types::RefreshMentalModelOperationDetails = serde_json::from_value(json!({
            "operation_type": "refresh_mental_model", "outcome": "refresh_failed_delta_not_applied",
            "failure_reason": reason
        }))
        .expect("refresh failure reasons must not alias OCR failure reasons");
        assert_eq!(
            serde_json::to_value(details).unwrap()["failure_reason"],
            reason
        );
    }
    for reason in [
        "refusal_or_no_text_response",
        "no_meaningful_text",
        "excessive_uncertainty",
        "excessive_repetition",
        "ui_chrome_only",
        "empty_content",
    ] {
        let details: types::FileConvertRetainOperationDetails = serde_json::from_value(json!({
            "operation_type": "file_convert_retain", "failure_class": "low_quality_ocr",
            "failure_reason": reason
        }))
        .expect("OCR failure reasons must not alias refresh failure reasons");
        assert_eq!(
            serde_json::to_value(details).unwrap()["failure_reason"],
            reason
        );
    }
}
