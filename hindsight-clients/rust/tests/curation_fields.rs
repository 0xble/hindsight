use hindsight_client::{CurationFields, NullablePatch};
use serde_json::json;

#[test]
fn correction_body_distinguishes_missing_clear_and_value() {
    let omitted = CurationFields { text: Some("canonical correction".into()), ..Default::default() };
    assert_eq!(serde_json::to_value(&omitted).unwrap(), json!({"text":"canonical correction"}));
    let clear = CurationFields { context: NullablePatch::Clear, occurred_start: NullablePatch::Clear, ..omitted.clone() };
    assert_eq!(serde_json::to_value(&clear).unwrap(), json!({"text":"canonical correction","context":null,"occurred_start":null}));
    let replaced = CurationFields { context: NullablePatch::Value("source context".into()), ..omitted };
    assert_eq!(serde_json::to_value(&replaced).unwrap()["context"], "source context");
    let decoded: CurationFields = serde_json::from_value(json!({"context":null})).unwrap();
    assert_eq!(decoded.context, NullablePatch::Clear);
    assert_eq!(decoded.occurred_start, NullablePatch::Unset);
}

#[test]
fn correction_body_serializes_both_dates() {
    use chrono::TimeZone;
    let fields = CurationFields {
        occurred_start: NullablePatch::Value(chrono::Utc.with_ymd_and_hms(2024, 1, 1, 0, 0, 0).unwrap()),
        occurred_end: NullablePatch::Value(chrono::Utc.with_ymd_and_hms(2024, 1, 2, 0, 0, 0).unwrap()),
        ..Default::default()
    };
    assert_eq!(
        serde_json::to_value(&fields).unwrap(),
        json!({"occurred_start":"2024-01-01T00:00:00Z","occurred_end":"2024-01-02T00:00:00Z"})
    );
}
