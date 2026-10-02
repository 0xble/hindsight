package hindsight

// SetContextNil explicitly clears the stored context.
func (o *CurationFields) SetContextNil() { o.Context.Set(nil) }

// SetOccurredStartNil explicitly clears the stored event start.
func (o *CurationFields) SetOccurredStartNil() { o.OccurredStart.Set(nil) }

// SetOccurredEndNil explicitly clears the stored event end.
func (o *CurationFields) SetOccurredEndNil() { o.OccurredEnd.Set(nil) }

// UnsetContext leaves the stored context absent.
func (o *CurationFields) UnsetContext() { o.Context.Unset() }

// UnsetOccurredStart leaves the stored event start absent.
func (o *CurationFields) UnsetOccurredStart() { o.OccurredStart.Unset() }

// UnsetOccurredEnd leaves the stored event end absent.
func (o *CurationFields) UnsetOccurredEnd() { o.OccurredEnd.Unset() }
