export interface Card {
  card_id: string;
  product: "CREDIT" | "DEBIT" | "PREPAID";
  last4: string;
  expiry: string;
  status: "ACTIVE" | "LOST" | "CLOSED";
  limit: number;
  available: number;
  last_used_at: string | null;
}

export type EnrollmentStatus = "PENDING" | "ENROLLED" | "SUSPENDED" | "REMOVED" | "FAILED";

export interface WalletCard {
  card_id: string;
  product: Card["product"];
  last4: string;
  expiry: string;
  card_status: Card["status"];
  eligible: boolean;
  ineligible_reason: string | null;
  enrollment: {
    enrollment_id: string;
    status: EnrollmentStatus;
    status_reason: string | null;
    failure_reason: string | null;
    updated_at: string;
  } | null;
}

export interface Wallet {
  customer_id: string;
  opted_out: boolean;
  terms_version: string | null;
  cards: WalletCard[];
}

export type TxnStatus =
  | "AUTHORIZED"
  | "DECLINED"
  | "SETTLED"
  | "PARTIALLY_REVERSED"
  | "REVERSED"
  | "REFUNDED"
  | "PARTIALLY_REFUNDED";

export const TXN_STATUSES: TxnStatus[] = [
  "AUTHORIZED",
  "DECLINED",
  "SETTLED",
  "PARTIALLY_REVERSED",
  "REVERSED",
  "REFUNDED",
  "PARTIALLY_REFUNDED",
];

export const DISPUTABLE: TxnStatus[] = [
  "AUTHORIZED",
  "SETTLED",
  "PARTIALLY_REVERSED",
  "PARTIALLY_REFUNDED",
];

export interface Transaction {
  txn_id: string;
  trace_id: string;
  card_id: string;
  merchant_name: string | null;
  mcc: string | null;
  status: TxnStatus;
  decline_code: string | null;
  amount: number | null;
  cleared_amount: number;
  reversed_amount: number;
  refunded_amount: number;
  currency: string;
  occurred_at: string;
  settled_at: string | null;
  open_dispute_id: string | null;
}

export interface Dispute {
  dispute_id: string;
  txn_id: string;
  reason: string;
  note: string | null;
  status: "OPEN";
  receipt: { filename: string; content_type: string; size: number } | null;
  created_at: string;
}

export interface Profile {
  email: string | null;
  email_verified: boolean;
  phone: string | null;
  phone_verified: boolean;
  version: number;
}
