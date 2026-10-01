import { useEffect, useId, useMemo, useRef, useState } from "react";

export type ComboboxOption = {
  value: string;
  label: string;
  searchText?: string;
  disabled?: boolean;
};

type Props = {
  value: string | null;
  options: ComboboxOption[];
  onChange: (value: string | null) => void;
  label: string;
  placeholder?: string;
  disabled?: boolean;
  loading?: boolean;
  error?: string | null;
  clearable?: boolean;
  emptyLabel?: string;
  selectedOption?: ComboboxOption | null;
  required?: boolean;
  autoFocus?: boolean;
};

function normalize(value: string) {
  return value.toLocaleLowerCase("fr-CA");
}

function optionMatches(option: ComboboxOption, query: string) {
  const normalizedQuery = normalize(query.trim());
  if (!normalizedQuery) return true;
  return normalize(`${option.label} ${option.searchText ?? ""}`).includes(normalizedQuery);
}

function firstEnabledIndex(options: ComboboxOption[]) {
  return options.findIndex((option) => !option.disabled);
}

function nextEnabledIndex(
  options: ComboboxOption[],
  currentIndex: number,
  direction: 1 | -1,
) {
  if (options.length === 0) return -1;
  for (let step = 1; step <= options.length; step += 1) {
    const candidate = (currentIndex + direction * step + options.length) % options.length;
    if (!options[candidate]?.disabled) return candidate;
  }
  return -1;
}

export default function SearchableCombobox({
  value,
  options,
  onChange,
  label,
  placeholder = "Sélectionner…",
  disabled = false,
  loading = false,
  error = null,
  clearable = false,
  emptyLabel = "Aucun résultat",
  selectedOption = null,
  required = false,
  autoFocus = false,
}: Props) {
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listboxId = useId();
  const errorId = useId();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [highlightedIndex, setHighlightedIndex] = useState(-1);

  const filteredOptions = useMemo(
    () => options.filter((option) => optionMatches(option, query)),
    [options, query],
  );
  const activeSelectedOption = options.find((option) => option.value === value) ?? null;
  const resolvedSelectedOption = activeSelectedOption
    ?? (selectedOption?.value === value ? selectedOption : null);
  const selectedLabel = resolvedSelectedOption?.label ?? "";

  useEffect(() => {
    if (!open) return;
    const selectedIndex = filteredOptions.findIndex(
      (option) => option.value === value && !option.disabled,
    );
    if (highlightedIndex >= 0 && filteredOptions[highlightedIndex] && !filteredOptions[highlightedIndex].disabled) {
      return;
    }
    setHighlightedIndex(selectedIndex >= 0 ? selectedIndex : firstEnabledIndex(filteredOptions));
  }, [filteredOptions, highlightedIndex, open, value]);

  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (rootRef.current?.contains(event.target as Node)) return;
      setOpen(false);
      setQuery("");
      setHighlightedIndex(-1);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [open]);

  function openPopup() {
    if (disabled || loading) return;
    setQuery("");
    setOpen(true);
    const selectedIndex = options.findIndex(
      (option) => option.value === value && !option.disabled,
    );
    setHighlightedIndex(selectedIndex >= 0 ? selectedIndex : firstEnabledIndex(options));
  }

  function closePopup() {
    setOpen(false);
    setQuery("");
    setHighlightedIndex(-1);
  }

  function choose(option: ComboboxOption) {
    if (option.disabled) return;
    onChange(option.value);
    closePopup();
    requestAnimationFrame(() => inputRef.current?.focus());
  }

  function handleQuery(valueToSearch: string) {
    if (!open) setOpen(true);
    setQuery(valueToSearch);
    const matching = options.filter((option) => optionMatches(option, valueToSearch));
    const selectedIndex = matching.findIndex(
      (option) => option.value === value && !option.disabled,
    );
    setHighlightedIndex(selectedIndex >= 0 ? selectedIndex : firstEnabledIndex(matching));
  }

  return (
    <div
      className={`searchable-combobox${open ? " is-open" : ""}${disabled || loading ? " is-disabled" : ""}${resolvedSelectedOption?.disabled ? " has-historical-value" : ""}${error ? " has-error" : ""}`}
      ref={rootRef}
    >
      <div className="searchable-combobox-control">
        <input
          ref={inputRef}
          className="searchable-combobox-input"
          type="text"
          role="combobox"
          aria-label={label}
          aria-expanded={open}
          aria-controls={listboxId}
          aria-autocomplete="list"
          aria-activedescendant={
            open && highlightedIndex >= 0
              ? `${listboxId}-option-${highlightedIndex}`
              : undefined
          }
          aria-invalid={Boolean(error)}
          aria-errormessage={error ? errorId : undefined}
          aria-required={required || undefined}
          data-combobox-value={value ?? ""}
          value={open ? query : selectedLabel}
          placeholder={loading ? "Chargement…" : placeholder}
          disabled={disabled || loading}
          required={required}
          autoFocus={autoFocus}
          onClick={() => {
            if (!open) openPopup();
          }}
          onChange={(event) => handleQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") {
              event.preventDefault();
              if (!open) {
                openPopup();
                return;
              }
              setHighlightedIndex((current) => (
                nextEnabledIndex(filteredOptions, current < 0 ? -1 : current, 1)
              ));
              return;
            }
            if (event.key === "ArrowUp") {
              event.preventDefault();
              if (!open) {
                openPopup();
                return;
              }
              setHighlightedIndex((current) => (
                nextEnabledIndex(
                  filteredOptions,
                  current < 0 ? filteredOptions.length : current,
                  -1,
                )
              ));
              return;
            }
            if (event.key === "Enter" && open && highlightedIndex >= 0) {
              const option = filteredOptions[highlightedIndex];
              if (option && !option.disabled) {
                event.preventDefault();
                choose(option);
              }
              return;
            }
            if (event.key === "Escape" && open) {
              event.preventDefault();
              event.stopPropagation();
              closePopup();
              return;
            }
            if (event.key === "Tab" && open) {
              closePopup();
            }
          }}
          onBlur={(event) => {
            const nextTarget = event.relatedTarget;
            if (nextTarget instanceof Node && rootRef.current?.contains(nextTarget)) return;
            closePopup();
          }}
        />
        <div className="searchable-combobox-actions">
          {clearable && value && !disabled && !loading && (
            <button
              type="button"
              className="searchable-combobox-clear"
              aria-label={`Effacer ${label}`}
              onMouseDown={(event) => event.preventDefault()}
              onClick={(event) => {
                event.stopPropagation();
                onChange(null);
                closePopup();
                inputRef.current?.focus();
              }}
            >
              ×
            </button>
          )}
          <button
            type="button"
            className="searchable-combobox-toggle"
            aria-label={open ? `Fermer ${label}` : `Ouvrir ${label}`}
            aria-expanded={open}
            disabled={disabled || loading}
            tabIndex={-1}
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => {
              if (open) closePopup();
              else {
                openPopup();
                inputRef.current?.focus();
              }
            }}
          >
            <span aria-hidden="true">{open ? "▴" : "▾"}</span>
          </button>
        </div>
      </div>

      {open && (
        <div
          id={listboxId}
          className="searchable-combobox-listbox"
          role="listbox"
          aria-label={`${label} options`}
        >
          {loading ? (
            <div className="searchable-combobox-message" role="status">Chargement…</div>
          ) : error ? (
            <div className="searchable-combobox-message is-error" role="alert">{error}</div>
          ) : filteredOptions.length === 0 ? (
            <div className="searchable-combobox-message" role="status">{emptyLabel}</div>
          ) : (
            filteredOptions.map((option, index) => (
              <div
                id={`${listboxId}-option-${index}`}
                className={`searchable-combobox-option${index === highlightedIndex ? " is-highlighted" : ""}${option.value === value ? " is-selected" : ""}${option.disabled ? " is-disabled" : ""}`}
                role="option"
                aria-selected={option.value === value}
                aria-disabled={option.disabled || undefined}
                key={option.value}
                onMouseEnter={() => {
                  if (!option.disabled) setHighlightedIndex(index);
                }}
                onMouseDown={(event) => {
                  event.preventDefault();
                  if (!option.disabled) choose(option);
                }}
              >
                {option.label}
              </div>
            ))
          )}
        </div>
      )}
      {error && <span className="searchable-combobox-error" id={errorId}>{error}</span>}
    </div>
  );
}
