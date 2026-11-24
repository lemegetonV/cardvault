"use client";

import { useBug } from "@/components/providers";

interface Props {
  name: string | null;
  testId: string;
  as?: "span" | "h1";
  id?: string;
  className?: string;
}

/** A merchant name. It is untrusted text, so React renders it as text and escapes it. */
export function Merchant({ name, testId, as: Tag = "span", id, className }: Props) {
  const unescaped = useBug("BUG_UI_UNESCAPED_MERCHANT");
  if (unescaped && name) {
    // Seeded bug: the name is parsed as HTML, so markup in it runs (XSS). (react/no-danger
    // only sees lowercase DOM tags, so it does not flag the dynamic tag here.)
    return (
      <Tag
        id={id}
        className={className}
        data-testid={testId}
        dangerouslySetInnerHTML={{ __html: name }}
      />
    );
  }
  return (
    <Tag id={id} className={className} data-testid={testId}>
      {name ?? "Pending details"}
    </Tag>
  );
}
