#!/bin/sh
# Export the AP model templates of a local Omada Controller container as JSON
# lines, for scripts/gen_omada_models.py. Run it against your own lab
# controller. The output holds device identifiers from TP-Link's templates:
# keep it outside the repository and never commit it.
#
# Usage: scripts/export-omada-models.sh CONTAINER > /some/where/ap_templates.jsonl
set -eu
container=${1:?usage: $0 CONTAINER}
java_file=$(mktemp "${TMPDIR:-/tmp}/OmadaModels.XXXXXX")
trap 'rm -f "$java_file"' EXIT
cat >"$java_file" <<'JAVA'
import com.mongodb.client.*;
import org.bson.Document;
public class OmadaModels {
    public static void main(String[] args) {
        try (MongoClient client = MongoClients.create("mongodb://127.0.0.1:27217")) {
            for (Document d : client.getDatabase("omada").getCollection("devicemodeltemplate")
                    .find(new Document("type", "ap"))) {
                d.remove("_id");
                System.out.println(d.toJson());
            }
        }
    }
}
JAVA
docker cp "$java_file" "$container:/tmp/OmadaModels.java"
docker exec -w /opt/tplink/EAPController/lib "$container" sh -c \
  'java -cp "$(ls mongodb-driver-sync-*.jar mongodb-driver-core-*.jar bson-*.jar | tr "\n" ":")" /tmp/OmadaModels.java 2>/dev/null'
