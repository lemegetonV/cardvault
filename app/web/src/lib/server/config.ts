// Server-only configuration, read at run time (not baked in at build time).
export const services = {
  auth: process.env.AUTH_URL ?? "http://auth:8001",
  card: process.env.CARD_URL ?? "http://card:8002",
  enrollment: process.env.ENROLLMENT_URL ?? "http://enrollment:8003",
  walletTxn: process.env.WALLET_TXN_URL ?? "http://wallet-txn:8004",
  profile: process.env.PROFILE_URL ?? "http://profile:8005",
};

export const operatorPublicUrl = (
  process.env.OPERATOR_PUBLIC_URL ?? "http://localhost:8101"
).replace(/\/$/, "");

// Secure cookies only outside localhost (spec 16.6 rule 41).
export const cookieSecure = (process.env.COOKIE_SECURE ?? "false").toLowerCase() === "true";

export const SESSION_SECONDS = 15 * 60; // spec 16.8 rule 53
