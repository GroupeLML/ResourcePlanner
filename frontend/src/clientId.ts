export type ClientCrypto = {
  randomUUID?: (() => string) | null;
  getRandomValues?: ((array: Uint8Array) => Uint8Array) | null;
};

let fallbackSequence = 0;

function uuidFromRandomBytes(bytes: Uint8Array) {
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join("-");
}

export function createClientId(
  cryptoApi: ClientCrypto | undefined = globalThis.crypto as ClientCrypto | undefined,
) {
  const randomUUID = cryptoApi?.randomUUID;
  if (typeof randomUUID === "function") {
    return randomUUID.call(cryptoApi);
  }

  const getRandomValues = cryptoApi?.getRandomValues;
  if (typeof getRandomValues === "function") {
    const bytes = new Uint8Array(16);
    getRandomValues.call(cryptoApi, bytes);
    return uuidFromRandomBytes(bytes);
  }

  fallbackSequence = (fallbackSequence + 1) >>> 0;
  const timestamp = Date.now().toString(36);
  const highResolution = typeof performance !== "undefined" && typeof performance.now === "function"
    ? Math.floor(performance.now() * 1000).toString(36)
    : "0";
  const random = Math.random().toString(36).slice(2, 14);
  return `rp-${timestamp}-${highResolution}-${fallbackSequence.toString(36)}-${random}`;
}
