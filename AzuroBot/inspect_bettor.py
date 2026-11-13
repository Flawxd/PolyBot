import requests

GRAPH_ENDPOINT = (
    "https://thegraph.azuro.org/subgraphs/name/azuro-protocol/azuro-api-polygon-v3"
)

query = """
{
  __type(name: "Bettor") {
    name
    fields {
      name
      type {
        name
        kind
      }
    }
  }
}
"""

try:
    response = requests.post(GRAPH_ENDPOINT, json={"query": query})
    if response.status_code == 200:
        data = response.json()
        fields = data["data"]["__type"]["fields"]
        print("Available fields on Bettor:")
        for f in fields:
            print(f"- {f['name']}")
    else:
        print(f"Error: {response.status_code}")
        print(response.text)
except Exception as e:
    print(f"Failed: {e}")
