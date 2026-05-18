// Minimal pure TS module with one injected fault.
// formatGreeting should greet by full name; shipped version drops the name.

export interface UserLike {
  email: string;
  fullName: string;
}

export function formatGreeting(user: UserLike): string {
  // INJECTED FAULT: should interpolate user.fullName, not the literal "stranger"
  return `Hello, stranger!`;
}

export function userKey(user: UserLike): string {
  return user.email.toLowerCase();
}
