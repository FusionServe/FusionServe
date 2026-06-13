import { useEffect, useRef, useState } from "react";

import type { ActiveFilter, ColumnMeta, FilterColumnMeta, OperatorMeta } from "@/lib/dataSchema";

/** Human-friendly labels for the introspected operator names (fallback: humanized). */
const OPERATOR_LABELS: Record<string, string> = {
  exact: "equals",
  neq: "not equals",
  isNull: "is null",
  inList: "in list",
  notInList: "not in list",
  contains: "contains",
  iContains: "contains (i)",
  startsWith: "starts with",
  iStartsWith: "starts with (i)",
  endsWith: "ends with",
  iEndsWith: "ends with (i)",
  regex: "matches regex",
  iRegex: "matches regex (i)",
  gt: "greater than",
  gte: "greater or equal",
  lt: "less than",
  lte: "less or equal",
  range: "between",
};

function operatorLabel(name: string): string {
  if (OPERATOR_LABELS[name]) return OPERATOR_LABELS[name];
  return name
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/_/g, " ")
    .toLowerCase();
}

/**
 * Per-column filter control: a funnel toggle in a column header that opens a
 * popover with an operator dropdown (all introspected operators for the
 * column's type) and a value input shaped to the operator (text/number/date,
 * boolean/enum select, comma-separated list, or a between-range pair).
 */
export function ColumnFilter({
  column,
  filterColumn,
  active,
  onApply,
  onClear,
}: {
  column: ColumnMeta;
  filterColumn: FilterColumnMeta;
  active: ActiveFilter | undefined;
  onApply: (filter: ActiveFilter) => void;
  onClear: () => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const operators = filterColumn.operators;
  const [op, setOp] = useState<string>(active?.op ?? operators[0]?.name ?? "");
  const opMeta = operators.find((o) => o.name === op) ?? operators[0];

  // Draft value, shaped per the selected operator's value kind.
  const [text, setText] = useState<string>(() => initialText(active));
  const [range, setRange] = useState<{ start: string; end: string }>(() => initialRange(active));
  const [bool, setBool] = useState<boolean>(() => active?.value === true || active?.value === "true");

  // Close on outside-click / Escape.
  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  function apply() {
    if (!opMeta) return;
    let value: unknown;
    switch (opMeta.valueKind) {
      case "boolean":
        value = bool;
        break;
      case "range":
        value = { start: range.start, end: range.end };
        break;
      default:
        value = text;
    }
    onApply({ op: opMeta.name, value });
    setOpen(false);
  }

  function clear() {
    onClear();
    setText("");
    setRange({ start: "", end: "" });
    setBool(false);
    setOpen(false);
  }

  const isActive = active !== undefined;

  return (
    <span ref={ref} className="relative inline-block">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        title={isActive ? "Edit filter" : "Filter column"}
        className={`ml-1 rounded p-0.5 align-middle transition-colors hover:bg-zinc-200 dark:hover:bg-zinc-700 ${
          isActive ? "text-blue-600 dark:text-blue-400" : "text-zinc-400"
        }`}
      >
        <FunnelIcon filled={isActive} />
      </button>
      {open && (
        <div className="absolute left-0 top-full z-30 mt-1 w-60 rounded-md border border-zinc-200 bg-white p-2.5 text-left shadow-lg dark:border-zinc-700 dark:bg-zinc-800">
          <label className="mb-1 block text-[11px] font-medium uppercase tracking-wide text-zinc-400">
            Operator
          </label>
          <select
            value={op}
            onChange={(e) => setOp(e.target.value)}
            className="mb-2 w-full rounded border border-zinc-300 bg-white px-1.5 py-1 text-sm dark:border-zinc-600 dark:bg-zinc-900"
          >
            {operators.map((o) => (
              <option key={o.name} value={o.name}>
                {operatorLabel(o.name)}
              </option>
            ))}
          </select>
          {opMeta && (
            <ValueInput
              column={column}
              op={opMeta}
              text={text}
              setText={setText}
              range={range}
              setRange={setRange}
              bool={bool}
              setBool={setBool}
              onEnter={apply}
            />
          )}
          <div className="mt-2.5 flex justify-end gap-2">
            <button
              type="button"
              onClick={clear}
              className="rounded px-2 py-1 text-xs font-medium text-zinc-500 hover:bg-zinc-100 dark:hover:bg-zinc-700"
            >
              Clear
            </button>
            <button
              type="button"
              onClick={apply}
              className="rounded-md bg-blue-600 px-2.5 py-1 text-xs font-medium text-white transition-colors hover:bg-blue-700"
            >
              Apply
            </button>
          </div>
        </div>
      )}
    </span>
  );
}

function ValueInput({
  column,
  op,
  text,
  setText,
  range,
  setRange,
  bool,
  setBool,
  onEnter,
}: {
  column: ColumnMeta;
  op: OperatorMeta;
  text: string;
  setText: (v: string) => void;
  range: { start: string; end: string };
  setRange: (v: { start: string; end: string }) => void;
  bool: boolean;
  setBool: (v: boolean) => void;
  onEnter: () => void;
}) {
  if (op.name === "isNull" || op.valueKind === "boolean") {
    return (
      <select
        value={bool ? "true" : "false"}
        onChange={(e) => setBool(e.target.value === "true")}
        className={fieldClass}
      >
        <option value="true">true</option>
        <option value="false">false</option>
      </select>
    );
  }

  if (op.valueKind === "range") {
    return (
      <div className="flex items-center gap-1.5">
        <input
          type={rangeInputType(op)}
          value={range.start}
          placeholder="start"
          onChange={(e) => setRange({ ...range, start: e.target.value })}
          className={fieldClass}
        />
        <span className="text-xs text-zinc-400">–</span>
        <input
          type={rangeInputType(op)}
          value={range.end}
          placeholder="end"
          onChange={(e) => setRange({ ...range, end: e.target.value })}
          className={fieldClass}
        />
      </div>
    );
  }

  if (op.valueKind === "list") {
    return (
      <input
        type="text"
        value={text}
        placeholder="comma,separated,values"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && onEnter()}
        className={fieldClass}
      />
    );
  }

  // Scalar: enum select when the column is an enum, else a typed input.
  if (column.editor === "enum") {
    return (
      <select value={text} onChange={(e) => setText(e.target.value)} className={fieldClass}>
        <option value="">—</option>
        {(column.enumValues ?? []).map((v) => (
          <option key={v} value={v}>
            {v}
          </option>
        ))}
      </select>
    );
  }
  const numeric = op.scalarType === "Int" || op.scalarType === "Float" || op.scalarType === "Decimal";
  return (
    <input
      type={numeric ? "number" : scalarInputType(column)}
      step={op.scalarType === "Int" ? 1 : "any"}
      value={text}
      placeholder="value"
      onChange={(e) => setText(e.target.value)}
      onKeyDown={(e) => e.key === "Enter" && onEnter()}
      className={fieldClass}
    />
  );
}

const fieldClass =
  "w-full rounded border border-zinc-300 bg-white px-1.5 py-1 text-sm outline-none focus:border-blue-400 dark:border-zinc-600 dark:bg-zinc-900";

function scalarInputType(column: ColumnMeta): string {
  if (column.editor === "date") return "date";
  if (column.editor === "datetime") return "datetime-local";
  return "text";
}

function rangeInputType(op: OperatorMeta): string {
  return op.scalarType === "Int" || op.scalarType === "Float" || op.scalarType === "Decimal"
    ? "number"
    : "text";
}

function initialText(active: ActiveFilter | undefined): string {
  if (!active) return "";
  if (Array.isArray(active.value)) return active.value.join(",");
  if (typeof active.value === "object" && active.value !== null) return "";
  if (typeof active.value === "boolean") return "";
  return String(active.value ?? "");
}

function initialRange(active: ActiveFilter | undefined): { start: string; end: string } {
  if (active && typeof active.value === "object" && active.value !== null && !Array.isArray(active.value)) {
    const r = active.value as { start?: unknown; end?: unknown };
    return { start: String(r.start ?? ""), end: String(r.end ?? "") };
  }
  return { start: "", end: "" };
}

function FunnelIcon({ filled }: { filled: boolean }) {
  return (
    <svg
      className="h-3.5 w-3.5"
      viewBox="0 0 24 24"
      fill={filled ? "currentColor" : "none"}
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z" />
    </svg>
  );
}
