// Small English helpers the views share.
export const pluralWord = (n, word) => (n === 1 ? word : `${word}s`);
export const plural = (n, word) => `${n} ${pluralWord(n, word)}`;
