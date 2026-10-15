/**
 * QuantumOrb personal monitor — initial Scout/Cartographer TypeScript contracts.
 *
 * These declarations correspond to coding-pack/database/SCHEMA.md and are safe
 * to consume from TypeScript or checked JavaScript with `// @ts-check`.
 * They describe read-only observation. No wallet signing or transaction
 * submission types are intentionally defined.
 */

declare namespace QuantumOrb {
  type IsoDateTime = string;
  type DecimalString = string;
  type JsonObject = Record<string, unknown>;
  type Address = string;
  type Signature = string;
  type Sha256 = string;
  type UUID = string;
  type DatabaseId = number;

  type Network = "mainnet-beta" | "devnet" | "testnet";
  type Commitment = "processed" | "confirmed" | "finalized";
  type Transport =
    | "parsed_streams"
    | "laserstream"
    | "logs_subscribe"
    | "parsed_events"
    | "helius_rpc"
    | "fixture";
  type ProgramName = "pump" | "pump_amm";
  type DecodeStatus = "decoded" | "raw_only" | "failed";
  type FinalityStatus = "provisional" | "finalized" | "reorged_or_changed";
  type ReconciliationStatus =
    | "not_required"
    | "pending"
    | "matched"
    | "changed"
    | "missing";
  type EvidenceState =
    | "observed"
    | "not_read"
    | "not_applicable"
    | "error"
    | "stale";
  type NodeValidationStatus = "valid" | "invalid" | "unknown";
  type GraphEdgeStrength = "direct" | "strong" | "moderate" | "weak" | "unknown";
  type GraphEdgeStatus = "active" | "expired" | "retracted" | "needs_review";

  type RawEventStatus =
    | "new"
    | "decoded"
    | "raw_only"
    | "ignored"
    | "failed";
  type LaunchDetectionStatus = "detected" | "enriched" | "scored" | "error";
  type TokenMetadataStatus = "unknown" | "partial" | "complete" | "failed";
  type TokenLifecycleState =
    | "seen_create"
    | "curve_active"
    | "curve_trading"
    | "curve_complete"
    | "migration_seen"
    | "pool_active"
    | "pool_trading"
    | "error";
  type ScoreClassification = "unknown" | "watch" | "candidate" | "blocked";
  type ScoreDecision = "observe" | "alert" | "suppress";
  type ScoreConfidence = "low" | "medium" | "high";
  type AlertChannel = "telegram";
  type AlertStatus = "pending" | "sent" | "failed" | "suppressed";
  type OutcomeState = "observed" | "not_available" | "error";
  type JobStatus = "running" | "success" | "partial_success" | "failed";
  type ReconciliationState = "pending" | "matched" | "changed" | "missing" | "failed";

  type ScoutEventType =
    | "token_created"
    | "curve_buy"
    | "curve_sell"
    | "curve_complete"
    | "migration"
    | "pool_created"
    | "pool_buy"
    | "pool_sell";

  type PumpEventName =
    | "CreateEvent"
    | "TradeEvent"
    | "CompleteEvent"
    | "PostCompleteBuyEvent"
    | "CompletePumpAmmMigrationEvent";
  type PumpAmmEventName = "CreatePoolEvent" | "BuyEvent" | "SellEvent";
  type EventName = PumpEventName | PumpAmmEventName;

  type GraphNodeType =
    | "wallet"
    | "token_mint"
    | "token_account"
    | "bonding_curve"
    | "pool"
    | "lp_mint"
    | "transaction"
    | "program"
    | "metadata_uri"
    | "bundle";

  type GraphEdgeType =
    | "created_token"
    | "user_created_token"
    | "creator_declared_for_token"
    | "owns_token_account"
    | "traded_token"
    | "funded_wallet"
    | "received_sol_from"
    | "received_token_from"
    | "shared_transaction"
    | "same_slot_activity"
    | "same_bundle_activity"
    | "shares_counterparty"
    | "controls_bonding_curve"
    | "associated_with_pool"
    | "coin_creator_for_pool"
    | "same_metadata_uri"
    | "fee_recipient_for";

  interface ProviderEnvelope {
    provider: "helius";
    transport: Transport;
    notificationId: string;
    receivedAt: IsoDateTime;
    commitment: Commitment;
    slot: number;
    signature: Signature;
    transactionError?: JsonObject | null;
    rawPayloadSha256: Sha256;
    idlRevision?: string;
    rawPayload: JsonObject;
  }

  interface DecodedAccount {
    name?: string;
    pubkey: Address;
    isSigner?: boolean;
    isWritable?: boolean;
  }

  interface DecodedInstruction {
    programId: Address;
    programName: ProgramName | "unknown";
    instructionName: string;
    instructionIndex: number;
    isInner: boolean;
    accounts: DecodedAccount[];
    args: JsonObject;
    matchedFilter: boolean;
  }

  interface DecodedEvent {
    eventName: EventName | string;
    programId: Address;
    eventIndex: number;
    fields: JsonObject;
    sourceInstructionIndex?: number;
    decodeStatus: DecodeStatus;
  }

  interface CanonicalSolanaEvent {
    eventKey: Sha256;
    signature: Signature;
    slot: number;
    blockTime?: IsoDateTime | null;
    observedAt: IsoDateTime;
    commitment: Commitment;
    program: ProgramName;
    programId: Address;
    eventName: EventName | string;
    eventType: ScoutEventType;
    mint?: Address | null;
    user?: Address | null;
    creator?: Address | null;
    bondingCurve?: Address | null;
    pool?: Address | null;
    quoteMint?: Address | null;
    transactionIndex?: number | null;
    instructionIndex?: number | null;
    eventIndex: number;
    payload: JsonObject;
    decodeStatus: DecodeStatus;
    finalityStatus: FinalityStatus;
    reconciliationStatus: ReconciliationStatus;
    derivationVersion: string;
    rawEventId?: DatabaseId;
  }

  interface RawEventRow {
    id: DatabaseId;
    eventKey: Sha256;
    provider: "helius";
    transport: Transport;
    notificationId: string;
    signature?: Signature | null;
    slot?: number | null;
    commitment?: Commitment | null;
    receivedAt: IsoDateTime;
    payloadJson: string;
    payloadSha256: Sha256;
    status: RawEventStatus;
    safeErrorCode?: string | null;
    safeErrorMessage?: string | null;
    idlRevision?: string | null;
    createdAt: IsoDateTime;
  }

  interface TokenRow {
    mint: Address;
    symbol?: string | null;
    name?: string | null;
    metadataUri?: string | null;
    decimals?: number | null;
    supplyRaw?: DecimalString | null;
    supplyUi?: string | null;
    mintAuthority?: Address | null;
    freezeAuthority?: Address | null;
    creatorWallet?: Address | null;
    firstSeenAt: IsoDateTime;
    lastSeenAt: IsoDateTime;
    metadataStatus: TokenMetadataStatus;
    lastIndexedSlot?: number | null;
    lastMetadataSource?: string | null;
    lastMetadataObservedAt?: IsoDateTime | null;
    rawAssetJson?: string | null;
    createdAt: IsoDateTime;
    updatedAt: IsoDateTime;
  }

  interface LaunchRow {
    id: DatabaseId;
    mint: Address;
    launchSignature: Signature;
    launchEventKey: Sha256;
    launchSlot: number;
    launchTime?: IsoDateTime | null;
    sourceProgram: ProgramName;
    creatorWallet?: Address | null;
    userWallet?: Address | null;
    bondingCurve?: Address | null;
    quoteMint?: Address | null;
    detectionStatus: LaunchDetectionStatus;
    firstDetectedAt: IsoDateTime;
    lastUpdatedAt: IsoDateTime;
    rawEventId: DatabaseId;
  }

  interface ScoutEventRow extends CanonicalSolanaEvent {
    id: DatabaseId;
    rawEventId: DatabaseId;
    createdAt: IsoDateTime;
  }

  interface TokenLifecycleRow {
    mint: Address;
    state: TokenLifecycleState;
    firstSeenSlot?: number | null;
    lastSeenSlot?: number | null;
    bondingCurve?: Address | null;
    pool?: Address | null;
    creator?: Address | null;
    user?: Address | null;
    quoteMint?: Address | null;
    provisional: boolean;
    dataGap: boolean;
    lastEventId?: DatabaseId | null;
    updatedAt: IsoDateTime;
  }

  interface EvidenceRow {
    id: DatabaseId;
    launchId: DatabaseId;
    category: "metadata" | "authority" | "creator" | "holders" | "wallet_cluster" | "liquidity" | "market";
    key: string;
    valueJson?: string | null;
    state: EvidenceState;
    source: string;
    sourceEndpoint?: string | null;
    sourceSignature?: Signature | null;
    observedAt: IsoDateTime;
    freshUntil?: IsoDateTime | null;
    sourceSlot?: number | null;
    errorCode?: string | null;
    errorMessageSafe?: string | null;
    algorithmVersion?: string | null;
    createdAt: IsoDateTime;
  }

  interface ScoreSnapshotRow {
    id: DatabaseId;
    launchId: DatabaseId;
    ruleVersion: string;
    score: number;
    classification: ScoreClassification;
    decision: ScoreDecision;
    confidence: ScoreConfidence;
    reasonJson: string;
    evidenceHash: Sha256;
    createdAt: IsoDateTime;
  }

  interface AlertRow {
    id: DatabaseId;
    launchId: DatabaseId;
    channel: AlertChannel;
    dedupeKey: string;
    status: AlertStatus;
    messageText: string;
    providerMessageId?: string | null;
    attemptCount: number;
    lastErrorSafe?: string | null;
    createdAt: IsoDateTime;
    sentAt?: IsoDateTime | null;
  }

  interface OutcomeRow {
    id: DatabaseId;
    launchId: DatabaseId;
    horizonMinutes: number;
    observedAt: IsoDateTime;
    priceReference?: string | null;
    marketCapReference?: string | null;
    volumeReference?: string | null;
    liquidityReference?: string | null;
    source: string;
    sourceTimestamp?: IsoDateTime | null;
    cacheAgeSeconds?: number | null;
    state: OutcomeState;
    rawJson?: string | null;
  }

  interface GraphNodeRow {
    id: DatabaseId;
    nodeKey: string;
    nodeType: GraphNodeType;
    addressOrIdentifier: string;
    network: Network;
    firstSeenAt: IsoDateTime;
    lastSeenAt: IsoDateTime;
    metadataJson?: string | null;
    validationStatus: NodeValidationStatus;
  }

  interface GraphEdgeRow {
    id: DatabaseId;
    sourceNodeId: DatabaseId;
    targetNodeId: DatabaseId;
    edgeType: GraphEdgeType;
    direction: "directed" | "undirected";
    strength: GraphEdgeStrength;
    status: GraphEdgeStatus;
    firstObservedAt: IsoDateTime;
    lastObservedAt: IsoDateTime;
    algorithmVersion: string;
  }

  interface GraphEdgeEvidenceRow {
    id: DatabaseId;
    edgeId: DatabaseId;
    evidenceType: string;
    signature?: Signature | null;
    slot?: number | null;
    eventKey?: Sha256 | null;
    source: string;
    detailsJson: string;
    observedAt: IsoDateTime;
    expiresAt?: IsoDateTime | null;
    algorithmVersion: string;
  }

  interface ProviderCatalogSnapshotRow {
    id: DatabaseId;
    programId: Address;
    programName?: string | null;
    idlRevision: string;
    payloadJson: string;
    contentSha256: Sha256;
    observedAt: IsoDateTime;
    sourceUrl: string;
    active: boolean;
  }

  interface ReconciliationRecordRow {
    id: DatabaseId;
    eventKey: Sha256;
    confirmedPayloadRef?: DatabaseId | null;
    finalizedPayloadRef?: DatabaseId | null;
    status: ReconciliationState;
    reason?: string | null;
    confirmedSlot?: number | null;
    finalizedSlot?: number | null;
    createdAt: IsoDateTime;
    resolvedAt?: IsoDateTime | null;
  }

  interface CursorRow {
    name: string;
    cursorJson: string;
    lastSuccessAt?: IsoDateTime | null;
    lastReceivedSlot?: number | null;
    lastReceivedSignature?: Signature | null;
    updatedAt: IsoDateTime;
  }

  interface JobRunRow {
    id: UUID;
    jobName: string;
    status: JobStatus;
    startedAt: IsoDateTime;
    completedAt?: IsoDateTime | null;
    countsJson: string;
    safeErrorCode?: string | null;
    safeErrorMessage?: string | null;
  }

  interface PumpCreateEventFields {
    name: string;
    symbol: string;
    uri: string;
    mint: Address;
    bonding_curve: Address;
    user: Address;
    creator: Address;
    timestamp: number;
    virtual_token_reserves: DecimalString;
    virtual_sol_reserves: DecimalString;
    real_token_reserves: DecimalString;
    token_total_supply: DecimalString;
    token_program?: Address;
    is_mayhem_mode?: boolean;
    is_cashback_enabled?: boolean;
    quote_mint?: Address;
    virtual_quote_reserves?: DecimalString;
    creator_fee_bps?: DecimalString;
    is_holder_reward?: boolean;
    depth?: number;
  }

  interface PumpTradeEventFields {
    mint: Address;
    sol_amount: DecimalString;
    token_amount: DecimalString;
    is_buy: boolean;
    user: Address;
    timestamp: number;
    virtual_sol_reserves: DecimalString;
    virtual_token_reserves: DecimalString;
    real_sol_reserves: DecimalString;
    real_token_reserves: DecimalString;
    fee_recipient?: Address;
    fee_basis_points?: DecimalString;
    fee?: DecimalString;
    creator?: Address;
    creator_fee_basis_points?: DecimalString;
    creator_fee?: DecimalString;
    quote_mint?: Address;
    quote_amount?: DecimalString;
    virtual_quote_reserves?: DecimalString;
    real_quote_reserves?: DecimalString;
    ix_name?: string;
    holder_rewards_bps?: DecimalString;
    holder_rewards?: DecimalString;
  }

  interface PumpAmmCreatePoolEventFields {
    timestamp: number;
    index: number;
    creator: Address;
    base_mint: Address;
    quote_mint: Address;
    base_mint_decimals: number;
    quote_mint_decimals: number;
    base_amount_in: DecimalString;
    quote_amount_in: DecimalString;
    pool_base_amount: DecimalString;
    pool_quote_amount: DecimalString;
    minimum_liquidity: DecimalString;
    initial_liquidity: DecimalString;
    lp_token_amount_out?: DecimalString;
    pool_bump?: number;
    pool: Address;
    lp_mint: Address;
    user_base_token_account?: Address;
    user_quote_token_account?: Address;
    coin_creator?: Address;
    is_mayhem_mode?: boolean;
    creator_fee_bps?: DecimalString;
    can_edit_creator_fee?: boolean;
    is_holder_reward?: boolean;
  }

  interface PumpAmmTradeEventFields {
    timestamp: number;
    base_amount_in?: DecimalString;
    base_amount_out?: DecimalString;
    min_quote_amount_out?: DecimalString;
    max_quote_amount_in?: DecimalString;
    quote_amount_in?: DecimalString;
    quote_amount_out?: DecimalString;
    user_base_token_reserves?: DecimalString;
    user_quote_token_reserves?: DecimalString;
    pool_base_token_reserves: DecimalString;
    pool_quote_token_reserves: DecimalString;
    lp_fee_basis_points?: DecimalString;
    lp_fee?: DecimalString;
    protocol_fee_basis_points?: DecimalString;
    protocol_fee?: DecimalString;
    pool: Address;
    user: Address;
    user_base_token_account?: Address;
    user_quote_token_account?: Address;
    protocol_fee_recipient?: Address;
    coin_creator?: Address;
    coin_creator_fee_basis_points?: DecimalString;
    coin_creator_fee?: DecimalString;
    virtual_quote_reserves: DecimalString;
    base_supply?: DecimalString;
    holder_rewards_bps?: DecimalString;
    holder_rewards?: DecimalString;
    ix_name?: string;
  }

  interface EvidenceResult<T = unknown> {
    key: string;
    state: EvidenceState;
    value?: T;
    source: string;
    sourceEndpoint?: string;
    sourceSignature?: Signature;
    observedAt: IsoDateTime;
    freshUntil?: IsoDateTime;
    sourceSlot?: number;
    safeErrorCode?: string;
    safeErrorMessage?: string;
    algorithmVersion?: string;
  }

  interface ScoutEventPublished {
    type: "ScoutEventPublished";
    schemaVersion: "0.1";
    eventKey: Sha256;
    mint?: Address | null;
    program: ProgramName;
    eventType: ScoutEventType;
    signature: Signature;
    slot: number;
    blockTime?: IsoDateTime | null;
    user?: Address | null;
    creator?: Address | null;
    pool?: Address | null;
    bondingCurve?: Address | null;
    payloadRef: DatabaseId;
    finality: FinalityStatus;
    derivationVersion: string;
  }

  interface CartographerCoverage {
    historyComplete: boolean;
    walletsScanned: number;
    transactionsScanned: number;
    lookbackDays?: number;
    maxWallets?: number;
    warnings: string[];
  }

  interface CartographerEvidenceUpdated {
    type: "CartographerEvidenceUpdated";
    schemaVersion: "0.1";
    runId: UUID;
    mint: Address;
    nodesAdded: number;
    edgesAdded: number;
    edgesRetracted: number;
    coverage: CartographerCoverage;
    /** UUID of the persisted job_runs summary; runId is also a lookup key. */
    summaryRef?: UUID | null;
    warnings: string[];
    algorithmVersion: string;
  }

  interface ScoutRepository {
    insertRawEventIfAbsent(event: ProviderEnvelope): Promise<RawEventRow>;
    upsertScoutEvent(event: CanonicalSolanaEvent): Promise<ScoutEventRow>;
    getScoutEventByKey(eventKey: Sha256): Promise<ScoutEventRow | null>;
    applyLifecycle(event: ScoutEventRow): Promise<TokenLifecycleRow>;
    getCursor(name: string): Promise<CursorRow | null>;
    commitCursor(cursor: CursorRow, jobRunId: UUID): Promise<void>;
  }

  interface CartographerRepository {
    upsertNode(node: Omit<GraphNodeRow, "id">): Promise<GraphNodeRow>;
    upsertEdge(edge: Omit<GraphEdgeRow, "id">): Promise<GraphEdgeRow>;
    appendEdgeEvidence(evidence: Omit<GraphEdgeEvidenceRow, "id">): Promise<GraphEdgeEvidenceRow>;
    getTokenSummary(mint: Address): Promise<CartographerEvidenceUpdated | null>;
  }

  interface ScoutAgent {
    start(): Promise<void>;
    stop(): Promise<void>;
    handleProviderEnvelope(envelope: ProviderEnvelope): Promise<void>;
    reconcileSignature(signature: Signature): Promise<void>;
  }

  interface CartographerAgent {
    start(): Promise<void>;
    stop(): Promise<void>;
    consumeScoutEvent(event: ScoutEventPublished): Promise<CartographerEvidenceUpdated>;
    analyzeWallet(wallet: Address, options?: WalletAnalysisOptions): Promise<CartographerEvidenceUpdated | null>;
  }

  interface WalletAnalysisOptions {
    mint?: Address;
    lookbackStart?: IsoDateTime;
    lookbackEnd?: IsoDateTime;
    maxTransactions?: number;
    maxCounterparties?: number;
    includeTokenAccounts?: boolean;
  }

  /** Adapter request; the RPC wire shape is [address, remaining options]. */
  interface HeliusWalletHistoryRequest {
    readonly address: Address;
    readonly transactionDetails: "full";
    readonly sortOrder: "asc" | "desc";
    /** RPC page size, from 1 through 1000. */
    readonly limit: number;
    readonly paginationToken?: string;
    readonly commitment: "confirmed" | "finalized";
    readonly encoding: "json";
    readonly maxSupportedTransactionVersion: 1;
    readonly filters: {
      /** Unix seconds: inclusive start and exclusive end. */
      readonly blockTime: { readonly gte: number; readonly lt: number };
      readonly status: "succeeded" | "failed" | "any";
      readonly tokenAccounts: "none" | "balanceChanged" | "all";
    };
  }
}

export = QuantumOrb;
