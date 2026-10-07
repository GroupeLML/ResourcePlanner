import type {
  BusinessContactReadModel,
  ContactResolutionReadModel,
} from "./api";
import SearchableCombobox, { type ComboboxOption } from "./SearchableCombobox";

export function contactLabel(
  contacts: BusinessContactReadModel[],
  contactId: string | null | undefined,
) {
  if (!contactId) return "Hérité / non défini";
  const contact = contacts.find((row) => row.id === contactId);
  if (!contact) return `Référence inconnue · ${contactId}`;
  return `${contact.display_name}${contact.active ? "" : " · inactif"}`;
}

export function ContactSelect({
  contacts,
  value,
  onChange,
  label,
  disabled = false,
  inheritLabel = "Hériter",
}: {
  contacts: BusinessContactReadModel[];
  value: string | null;
  onChange: (value: string | null) => void;
  label: string;
  disabled?: boolean;
  inheritLabel?: string;
}) {
  const inheritValue = "__resourceplanner_contact_inherit__";
  const selectedContact = value
    ? contacts.find((contact) => contact.id === value) ?? null
    : null;
  const options: ComboboxOption[] = [
    { value: inheritValue, label: inheritLabel },
    ...contacts
      .filter((contact) => contact.active)
      .map((contact) => ({
        value: contact.id,
        label: contact.display_name,
        searchText: [contact.display_name, contact.email, contact.phone]
          .filter(Boolean)
          .join(" "),
      })),
  ];
  const historicalOption: ComboboxOption | null = value && (!selectedContact || !selectedContact.active)
    ? {
        value,
        label: selectedContact
          ? `${selectedContact.display_name} · inactif`
          : `Référence inconnue · ${value}`,
        disabled: true,
      }
    : null;

  return (
    <SearchableCombobox
      value={value ?? inheritValue}
      options={options}
      onChange={(nextValue) => {
        onChange(!nextValue || nextValue === inheritValue ? null : nextValue);
      }}
      label={label}
      disabled={disabled}
      placeholder={inheritLabel}
      emptyLabel="Aucun contact correspondant"
      selectedOption={historicalOption}
    />
  );
}

const SOURCE_LABELS: Record<string, string> = {
  REQUEST_OVERRIDE: "Override de la demande",
  TASK_RESPONSIBLE: "Responsable de la tâche",
  PROJECT_MANAGER: "Chargé de projet",
  RESOURCE_COORDINATOR: "Coordonnateur de la ressource",
  TASK_COORDINATOR: "Coordonnateur de la tâche",
  NONE: "Aucune source",
};

const DIAGNOSTIC_LABELS: Record<string, string> = {
  CONTACT_REFERENCE_INVALID: "Référence de contact invalide",
  CONTACT_INACTIVE: "Contact inactif",
  CONTACT_EMAIL_MISSING: "Courriel non renseigné",
  CONTACT_PHONE_MISSING: "Téléphone non renseigné",
  CONTACT_UNRESOLVED: "Aucun contact résolu",
  TASK_REFERENCE_INVALID: "Référence de tâche invalide",
  TASK_REFERENCE_LEGACY_CODE: "Tâche retrouvée par son code historique",
  TASK_REFERENCE_UNRESOLVED: "Tâche non résolue",
  TASK_PROJECT_MISMATCH: "Tâche rattachée à un autre projet",
  TASK_INACTIVE: "Tâche inactive",
  RESOURCE_REFERENCE_INVALID: "Ressource proposée introuvable",
  RESOURCE_INACTIVE: "Ressource proposée inactive",
  PROJECT_MANAGER_CONTACT_UNMIGRATED: "Chargé de projet historique sans contact métier lié",
};

function diagnosticText(values: string[]) {
  return values.map((value) => DIAGNOSTIC_LABELS[value] || value).join(" · ");
}

export function ResolutionSummary({
  title,
  resolution,
}: {
  title: string;
  resolution: ContactResolutionReadModel;
}) {
  const resolved = resolution.status === "RESOLVED";
  return (
    <div className={`contact-resolution ${resolved ? "is-resolved" : "is-warning"}`}>
      <span>{title}</span>
      <strong>{resolution.display_name || "Non résolu"}</strong>
      <small>
        Source : {SOURCE_LABELS[resolution.source_type] || resolution.source_type}
        {resolution.source_label ? ` · ${resolution.source_label}` : ""}
      </small>
      {(resolution.phone || resolution.email) && (
        <small>{[resolution.phone, resolution.email].filter(Boolean).join(" · ")}</small>
      )}
      {resolution.diagnostics.length > 0 && (
        <small className="contact-diagnostics">
          {diagnosticText(resolution.diagnostics)}
        </small>
      )}
    </div>
  );
}
