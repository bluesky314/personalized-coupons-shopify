// Calls the Python scoring microservice to get personalized coupon decisions.

const SCORING_SERVICE_URL =
  process.env.SCORING_SERVICE_URL || "http://localhost:8081";

export interface ScoringResult {
  tier: "SUPPRESS" | "NUDGE" | "FULL_COUPON";
  score: number;
  nudge_pct: number;
  full_pct: number;
  hard_gate: boolean;
  archetype: string;
  summary: string;
  key_risk_factors: any[];
  personalized_strategies: any[];
}

export async function scoreCustomer(
  customerData: Record<string, any>,
): Promise<ScoringResult> {
  const response = await fetch(`${SCORING_SERVICE_URL}/score`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(customerData),
  });

  if (!response.ok) {
    const error = await response.text();
    throw new Error(
      `Scoring service error: ${response.status} — ${error}`,
    );
  }

  return response.json();
}
