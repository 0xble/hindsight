package hindsight

// setCurationNull preserves explicit null separately from an omitted pointer.
func (o *CurationFields) setCurationNull(key string) {
	if o.AdditionalProperties == nil {
		o.AdditionalProperties = map[string]interface{}{}
	}
	o.AdditionalProperties[key] = nil
}

// SetContextNil explicitly clears the stored context.
func (o *CurationFields) SetContextNil() { o.Context = nil; o.setCurationNull("context") }

// SetOccurredStartNil explicitly clears the stored event start.
func (o *CurationFields) SetOccurredStartNil() {
	o.OccurredStart = nil
	o.setCurationNull("occurred_start")
}

// SetOccurredEndNil explicitly clears the stored event end.
func (o *CurationFields) SetOccurredEndNil() { o.OccurredEnd = nil; o.setCurationNull("occurred_end") }

// UnsetContext leaves the stored context unchanged.
func (o *CurationFields) UnsetContext() { o.Context = nil; delete(o.AdditionalProperties, "context") }

// UnsetOccurredStart leaves the stored event start unchanged.
func (o *CurationFields) UnsetOccurredStart() {
	o.OccurredStart = nil
	delete(o.AdditionalProperties, "occurred_start")
}

// UnsetOccurredEnd leaves the stored event end unchanged.
func (o *CurationFields) UnsetOccurredEnd() {
	o.OccurredEnd = nil
	delete(o.AdditionalProperties, "occurred_end")
}
