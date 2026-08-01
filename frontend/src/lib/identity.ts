/** Product name and user-initial helpers, shared by the sidebar and the header. */

export const APP_NAME = 'C.L.E.A.R.';

export const initialsOf = (name?: string | null) =>
  (name ?? '')
    .split(' ')
    .map((part) => part[0])
    .join('')
    .slice(0, 2)
    .toUpperCase();
