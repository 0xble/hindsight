"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from "@/components/ui/select";
import { Plus, Trash2 } from "lucide-react";
import {
  SchemaField,
  SchemaFieldType,
  SchemaItemType,
  SCHEMA_FIELD_TYPES,
  SCHEMA_ITEM_TYPES,
  validateResponseSchema,
  fieldsToSchema,
  schemaToFields,
  emptyField,
} from "@/lib/response-schema";

type Mode = "visual" | "code";

/**
 * No-code editor for a structured-output `response_schema`. Visual mode edits a
 * flat list of top-level fields; Code mode edits the raw JSON. The two stay in
 * sync on tab switch, and Apply is blocked until the current representation is a
 * usable schema (same contract as the backend's validate_response_schema()).
 */
export function SchemaBuilderDialog({
  open,
  value,
  onClose,
  onSave,
}: {
  open: boolean;
  value: string;
  onClose: () => void;
  onSave: (json: string) => void;
}) {
  const t = useTranslations("schemaBuilder");
  const [mode, setMode] = useState<Mode>("visual");
  const [fields, setFields] = useState<SchemaField[]>([emptyField()]);
  const [code, setCode] = useState("");
  // Set only when a Code→Visual switch fails (invalid or too-complex JSON); cleared on any edit.
  const [switchError, setSwitchError] = useState<string | null>(null);

  // Seed the editor from the incoming value each time the dialog opens.
  useEffect(() => {
    if (!open) return;
    setSwitchError(null);
    const text = value.trim();
    if (!text) {
      setMode("visual");
      setFields([emptyField()]);
      setCode("");
      return;
    }
    try {
      const parsed = JSON.parse(text);
      const asFields =
        typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
          ? schemaToFields(parsed as Record<string, unknown>)
          : null;
      if (asFields !== null) {
        setFields(asFields.length > 0 ? asFields : [emptyField()]);
        setMode("visual");
      } else {
        setMode("code");
      }
      setCode(JSON.stringify(parsed, null, 2));
    } catch {
      // Not valid JSON — let the user fix it in code mode.
      setMode("code");
      setCode(text);
    }
  }, [open, value]);

  const updateField = (index: number, patch: Partial<SchemaField>) =>
    setFields((fs) => fs.map((f, i) => (i === index ? { ...f, ...patch } : f)));
  const removeField = (index: number) => setFields((fs) => fs.filter((_, i) => i !== index));
  const addField = () => setFields((fs) => [...fs, emptyField()]);

  // Validate the current representation; returns a message or null.
  const visualError = (() => {
    const named = fields.filter((f) => f.name.trim());
    if (named.length === 0) return t("emptyState");
    const names = named.map((f) => f.name.trim());
    if (new Set(names).size !== names.length) return t("duplicateNames");
    return validateResponseSchema(fieldsToSchema(fields));
  })();

  const codeError = (() => {
    const text = code.trim();
    if (!text) return t("emptyState");
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      return t("invalidJson");
    }
    return validateResponseSchema(parsed);
  })();

  const currentError = mode === "visual" ? visualError : codeError;

  const switchTo = (next: Mode) => {
    if (next === mode) return;
    setSwitchError(null);
    if (next === "code") {
      setCode(JSON.stringify(fieldsToSchema(fields), null, 2));
      setMode("code");
      return;
    }
    // Code → Visual: only allowed when the JSON maps cleanly to flat fields.
    let parsed: unknown;
    try {
      parsed = JSON.parse(code);
    } catch {
      setSwitchError(t("invalidJson"));
      return;
    }
    const asFields =
      typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
        ? schemaToFields(parsed as Record<string, unknown>)
        : null;
    if (asFields === null) {
      setSwitchError(t("visualUnavailable"));
      return;
    }
    // An empty schema is representable — start the visual editor with a blank field.
    setFields(asFields.length > 0 ? asFields : [emptyField()]);
    setMode("visual");
  };

  const handleApply = () => {
    if (mode === "visual") {
      if (visualError) return;
      onSave(JSON.stringify(fieldsToSchema(fields), null, 2));
    } else {
      if (codeError) return;
      onSave(JSON.stringify(JSON.parse(code), null, 2));
    }
    onClose();
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-2xl max-h-[90vh] flex flex-col">
        <DialogHeader>
          <DialogTitle>{t("title")}</DialogTitle>
          <DialogDescription>{t("subtitle")}</DialogDescription>
        </DialogHeader>

        <Tabs
          value={mode}
          onValueChange={(v) => switchTo(v as Mode)}
          className="flex-1 flex flex-col min-h-0 overflow-hidden"
        >
          <TabsList className="w-full">
            <TabsTrigger value="visual" className="flex-1">
              {t("visualTab")}
            </TabsTrigger>
            <TabsTrigger value="code" className="flex-1">
              {t("codeTab")}
            </TabsTrigger>
          </TabsList>

          <div className="flex-1 overflow-y-auto mt-3 px-0.5">
            <TabsContent value="visual" className="space-y-3">
              {fields.length === 0 ? (
                <p className="text-sm text-muted-foreground py-2">{t("emptyState")}</p>
              ) : (
                fields.map((field, i) => (
                  <div key={i} className="rounded-lg border border-border p-3 space-y-2">
                    <div className="flex items-center gap-2">
                      <Input
                        value={field.name}
                        onChange={(e) => updateField(i, { name: e.target.value })}
                        placeholder={t("namePlaceholder")}
                        className="flex-1 h-8"
                      />
                      <Select
                        value={field.type}
                        onValueChange={(v) => updateField(i, { type: v as SchemaFieldType })}
                      >
                        <SelectTrigger className="w-28 h-8">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {SCHEMA_FIELD_TYPES.map((ty) => (
                            <SelectItem key={ty} value={ty}>
                              {ty}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                      {field.type === "array" && (
                        <Select
                          value={field.itemType}
                          onValueChange={(v) => updateField(i, { itemType: v as SchemaItemType })}
                        >
                          <SelectTrigger className="w-24 h-8">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            {SCHEMA_ITEM_TYPES.map((ty) => (
                              <SelectItem key={ty} value={ty}>
                                {ty}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      )}
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8 shrink-0"
                        onClick={() => removeField(i)}
                        aria-label={t("removeField")}
                      >
                        <Trash2 className="w-4 h-4" />
                      </Button>
                    </div>
                    <div className="flex items-center gap-3">
                      <Input
                        value={field.description}
                        onChange={(e) => updateField(i, { description: e.target.value })}
                        placeholder={t("descriptionPlaceholder")}
                        className="flex-1 h-8 text-xs"
                      />
                      <label className="flex items-center gap-1.5 text-xs cursor-pointer whitespace-nowrap">
                        <Checkbox
                          checked={field.required}
                          onCheckedChange={(c) => updateField(i, { required: c === true })}
                        />
                        {t("required")}
                      </label>
                    </div>
                  </div>
                ))
              )}
              <Button variant="outline" size="sm" onClick={addField}>
                <Plus className="w-4 h-4 mr-1" />
                {t("addField")}
              </Button>
            </TabsContent>

            <TabsContent value="code">
              <Textarea
                value={code}
                onChange={(e) => {
                  setCode(e.target.value);
                  setSwitchError(null);
                }}
                rows={14}
                className="font-mono text-xs"
                placeholder='{"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}'
              />
            </TabsContent>
          </div>
        </Tabs>

        {(switchError || currentError) && (
          <p className="text-xs text-destructive">{switchError || currentError}</p>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            {t("cancel")}
          </Button>
          <Button onClick={handleApply} disabled={!!currentError}>
            {t("apply")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
