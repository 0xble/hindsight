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
