// Small English helpers the views share.
export const pluralWord = (n, word) => (n === 1 ? word : `${word}s`);
export const plural = (n, word) => `${n} ${pluralWord(n, word)}`;
// "A", "A and B", "A, B and C".
export const andList = (xs) => (xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs.at(-1)}`);
