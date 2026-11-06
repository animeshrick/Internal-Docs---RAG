// Supabase Edge Function: turns text into vectors with the built-in gte-small model.
// The model runs on Supabase's servers, so there is no extra API key and nothing to host yourself.
//
// Deploy once (needs the Supabase CLI):
//     supabase functions deploy embed --no-verify-jwt
// --no-verify-jwt is needed because the new secret keys are not JWTs. The function checks the key itself below.
//
// Request  : POST { "inputs": ["text one", "text two"] }
// Response : { "embeddings": [[...384 numbers...], [...384 numbers...]] }
import "jsr:@supabase/functions-js/edge-runtime.d.ts";

const model = new Supabase.ai.Session("gte-small");

Deno.serve(async (request: Request) => {
  // Only callers that send one of this project's secret keys in the "apikey" header may use the function.
  const secretKeys = Object.values(JSON.parse(Deno.env.get("SUPABASE_SECRET_KEYS") ?? "{}"));
  const callerKey = request.headers.get("apikey");
  if (!callerKey || !secretKeys.includes(callerKey)) {
    return new Response("Unauthorized", { status: 401 });
  }

  const { inputs } = await request.json();

  const embeddings: number[][] = [];
  for (const text of inputs) {
    // mean_pool + normalize gives one vector per text, scaled so cosine search works well.
    const embedding = await model.run(text, { mean_pool: true, normalize: true });
    embeddings.push(Array.from(embedding as Iterable<number>));
  }

  return new Response(JSON.stringify({ embeddings }), {
    headers: { "Content-Type": "application/json" },
  });
});
