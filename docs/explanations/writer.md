# Architecture of the TiledWriter Callback

To ingest Bluesky data into Tiled, `TiledWriter` callback is subscribed to the
Bluesky Run Engine.

Structurally, `TiledWriter` consists of two main parts: `RunNormalizer` and
`_RunWriter`.

The former is responsible for converting legacy document schemas to their latest
version; this ensures that existing Bluesky code that relies on older versions
of the Bluesky Event Model can still function correctly with `TiledWriter`. For
example, while `TiledWriter` natively supports the modern `StreamResource` and
`StreamDatum` documents (commonly used in asynchronous plans), the `Resource`
and `Datum` documents are automatically converted to their modern counterparts
prior to being written to the Tiled catalog. The schema normalization is mostly
done by renaming and restructuring certain document fields, but subclassing
`RunNormalizer` also allows the user to invoke use-case-specific patches for
each type of document and achieve high flexibility.

The simplified flowchart of the `RunNormalizer` logic is shown below. It
illustrates how the input documents (top) are processed and emitted as output
documents (bottom) after specific transformations or caching operations.

```{mermaid}
flowchart TD
    %% Input documents
    subgraph Input [ ]
        style Input fill:#ffffff,stroke-width:0
        StartIn["Start"]
        DescriptorIn["Descriptor"]
        ResourceIn["Resource"]
        DatumIn["Datum"]
        EventIn["Event"]
        StopIn["Stop"]
    end

    %% Emitted documents
    subgraph Output [ ]
        style Output fill:#ffffff,stroke-width:0
        StartOut["Start"]
        DescriptorOut["Descriptor"]
        EventOut["Event"]
        StreamResourceOut["StreamResource"]
        StreamDatumOut["StreamDatum"]
        StopOut["Stop"]
    end

    %% Processing steps
    StartIn --> P1["start():<br/>patch → emit"]
    P1 --> StartOut

    DescriptorIn --> P2["descriptor():<br/>patch → rename fields →<br/>track internal/external keys → emit"]
    P2 --> DescriptorOut

    ResourceIn --> P3["resource():<br/>patch → convert to StreamResource → cache"]
    P3 --> SResCache[(SRes Cache)]

    DatumIn --> P4["datum():<br/>patch → cache"]
    P4 --> DatumCache[(Datum Cache)]

    EventIn --> P5["event():<br/>patch → split internal/external keys → emit"]
    P5 -->|internal data| EventOut
    P5 -->|external data| P6["convert_datum_to_stream_datum()<br/>move datum_kwargs to parameters on SRes"]
    P6 --> StreamDatumOut
    P6 --> |only before first SDatum| StreamResourceOut

    StopIn --> P7["stop():<br/>patch → flush cached StreamDatum"]
    P7 --> StopOut
    P7 --> StreamDatumOut
    P7 --> |if not emitted<br/>already| StreamResourceOut

    %% Extra connections
    SResCache --> P6
    DatumCache --> P6

    %% Styling
    classDef doc fill:#e0f7fa,stroke:#00796b,stroke-width:1px;
    classDef emit fill:#f1f8e9,stroke:#33691e,stroke-width:1px;
    classDef proc fill:#fff3e0,stroke:#e65100,stroke-width:1px;

    class StartIn,DescriptorIn,ResourceIn,DatumIn,EventIn,StopIn doc;
    class StartOut,DescriptorOut,EventOut,StreamResourceOut,StreamDatumOut,StopOut emit;
    class P1,P2,P3,P4,P5,P6,P7 proc;
```

The second component, `_RunWriter`, is the callback that directly communicates
with the Tiled server. It uses the `RunRouter` to manage the routing of
documents from multiple runs, ensuring that each Bluesky run is handled
separately.

Furthermore, `TiledWriter` implements a backup mechanism that allows saving the
documents to a local file system in case the Tiled server is not available or
any other error occurs during the writing process. This ensures that no data is
lost and the writing can be retried later.
