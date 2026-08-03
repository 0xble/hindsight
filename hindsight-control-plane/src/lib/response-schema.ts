/**
 * Helpers for the reflect / mental-model `response_schema` used by structured
 * output. The engine builds a flat Pydantic model from the schema's top-level
 * `properties` (see reflect/agent.py::_generate_structured_output), so the
 * usable contract is: an object schema with a non-empty `properties` map. These
 * helpers keep the frontend validation and the visual builder in lockstep with
 * that contract (mirrored on the backend by validate_response_schema()).
 */

export const SCHEMA_FIELD_TYPES = [
  "string",
  "number",
  "integer",
  "boolean",
  "array",
  "object",
] as const;

export type SchemaFieldType = (typeof SCHEMA_FIELD_TYPES)[number];

/** Item types allowed for an `array` field in the visual builder (scalars only). */
export const SCHEMA_ITEM_TYPES = ["string", "number", "integer", "boolean"] as const;
export type SchemaItemType = (typeof SCHEMA_ITEM_TYPES)[number];

/** One top-level property, as edited in the visual builder. */
export interface SchemaField {
  name: string;
  type: SchemaFieldType;
  /** Only meaningful when `type === "array"`. */
  itemType: SchemaItemType;
  description: string;
  required: boolean;
}

/**
 * Validate a parsed JSON value against the usable-schema contract.
 * Returns a human-readable error message, or `null` when the schema is usable.
 */
export function validateResponseSchema(schema: unknown): string | null {
  if (typeof schema !== "object" || schema === null || Array.isArray(schema)) {
    return "Schema must be a JSON object.";
  }
  const obj = schema as Record<string, unknown>;

  if (obj.type !== undefined && obj.type !== "object") {
    return 'Schema must be an object schema (its "type" must be "object").';
  }

  const properties = obj.properties;
  if (typeof properties !== "object" || properties === null || Array.isArray(properties)) {
    return "Schema must define a non-empty 'properties' object.";
  }
  const propEntries = Object.entries(properties as Record<string, unknown>);
  if (propEntries.length === 0) {
    return "Schema must define at least one property.";
  }

  for (const [name, prop] of propEntries) {
    if (typeof prop !== "object" || prop === null || Array.isArray(prop)) {
      return `Property '${name}' must be an object.`;
    }
    const propType = (prop as Record<string, unknown>).type;
    if (propType !== undefined && !SCHEMA_FIELD_TYPES.includes(propType as SchemaFieldType)) {
      return `Property '${name}' has unsupported type '${String(propType)}'.`;
    }
  }

  const required = obj.required;
  if (required !== undefined) {
    if (!Array.isArray(required) || !required.every((r) => typeof r === "string")) {
      return "'required' must be a list of property names.";
    }
    const propNames = new Set(propEntries.map(([n]) => n));
    const unknown = (required as string[]).filter((r) => !propNames.has(r));
    if (unknown.length > 0) {
      return `'required' references unknown properties: ${unknown.join(", ")}.`;
    }
  }

  return null;
}

/** Build a JSON Schema object from the visual builder's field list. */
export function fieldsToSchema(fields: SchemaField[]): Record<string, unknown> {
  const properties: Record<string, unknown> = {};
  const required: string[] = [];
  for (const field of fields) {
    const name = field.name.trim();
    if (!name) continue;
    const prop: Record<string, unknown> = { type: field.type };
    if (field.type === "array") {
      prop.items = { type: field.itemType };
    }
    if (field.description.trim()) {
      prop.description = field.description.trim();
    }
    properties[name] = prop;
    if (field.required) required.push(name);
  }
  const schema: Record<string, unknown> = { type: "object", properties };
  if (required.length > 0) schema.required = required;
  return schema;
}

/**
 * Convert a JSON Schema object into visual builder fields. Returns `null` when
 * the schema can't be represented losslessly in the flat visual editor (e.g. a
 * nested object with its own `properties`, or an array of non-scalars) — the
 * caller keeps the user in code mode so nothing is silently dropped.
 */
export function schemaToFields(schema: Record<string, unknown>): SchemaField[] | null {
  const properties = schema.properties;
  // A missing/empty `properties` is representable — it just means "no fields yet"
  // (an empty visual editor), which is distinct from a schema the flat editor
  // genuinely can't render (returns null below).
  if (properties === undefined) return [];
  if (typeof properties !== "object" || properties === null || Array.isArray(properties)) {
    return null;
  }
  const requiredSet = new Set(Array.isArray(schema.required) ? (schema.required as string[]) : []);
  const fields: SchemaField[] = [];
  for (const [name, rawProp] of Object.entries(properties as Record<string, unknown>)) {
    if (typeof rawProp !== "object" || rawProp === null || Array.isArray(rawProp)) return null;
    const prop = rawProp as Record<string, unknown>;
    const type = (prop.type as SchemaFieldType) ?? "string";
    if (!SCHEMA_FIELD_TYPES.includes(type)) return null;
    // Nested object with its own properties isn't representable as a flat field.
    if (type === "object" && prop.properties !== undefined) return null;
    let itemType: SchemaItemType = "string";
    if (type === "array") {
      const items = prop.items;
      if (typeof items === "object" && items !== null && !Array.isArray(items)) {
        const it = (items as Record<string, unknown>).type as SchemaItemType;
        if (it !== undefined && !SCHEMA_ITEM_TYPES.includes(it)) return null;
        if (it) itemType = it;
      } else if (items !== undefined) {
        return null;
      }
    }
    fields.push({
      name,
      type,
      itemType,
      description: typeof prop.description === "string" ? prop.description : "",
      required: requiredSet.has(name),
    });
  }
  return fields;
}

/** A single empty field for the visual builder. */
export function emptyField(): SchemaField {
  return { name: "", type: "string", itemType: "string", description: "", required: false };
}
