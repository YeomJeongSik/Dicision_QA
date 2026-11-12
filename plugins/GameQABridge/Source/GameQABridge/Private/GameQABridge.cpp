#include "Modules/ModuleManager.h"

#if PLATFORM_WINDOWS && UE_BUILD_DEVELOPMENT && !WITH_EDITOR
#include "HAL/PlatformMisc.h"
#include "HAL/PlatformTime.h"
#include "Misc/Char.h"
#include "Misc/Guid.h"
#include "Serialization/JsonSerializer.h"
#include "BridgeInput.h"
#include "Containers/Ticker.h"
#include <algorithm>

THIRD_PARTY_INCLUDES_START
#pragma push_macro("check")
#undef check
#include <boost/asio/ip/tcp.hpp>
#include <boost/beast/core/flat_buffer.hpp>
#include <boost/beast/core/tcp_stream.hpp>
#include <boost/beast/http.hpp>
#include <optional>
#include <thread>
#pragma pop_macro("check")
THIRD_PARTY_INCLUDES_END

#include "Windows/WindowsHWrapper.h"
#include <bcrypt.h>

namespace
{
namespace Asio = boost::asio;
namespace Beast = boost::beast;
namespace Http = Beast::http;
using Tcp = Asio::ip::tcp;
using Error = boost::system::error_code;
constexpr int32 LeaseMs = 750;

class FBridgeListener
{
public:
    FBridgeListener(const FString& Token, FBridgeInput& InInput)
        : BootstrapAuthorization(std::string("Bearer ") + TCHAR_TO_UTF8(*Token)), Input(InInput)
    {
    }

    bool Start()
    {
        Error Ec;
        Acceptor.open(Tcp::v4(), Ec);
        if (Ec) { return false; }
        Acceptor.bind({Asio::ip::address_v4::loopback(), 0}, Ec);
        if (Ec) { return false; }
        Acceptor.listen(4, Ec);
        if (Ec) { return false; }
        const auto Endpoint = Acceptor.local_endpoint(Ec);
        if (Ec) { return false; }

        Host = "127.0.0.1:" + std::to_string(Endpoint.port());
        Accept();
        Worker = std::thread([this] { Io.run(); });
        const std::string Line = "UNREALAUTOQA_PORT=" + std::to_string(Endpoint.port()) + "\n";
        DWORD Written = 0;
        return ::WriteFile(::GetStdHandle(STD_OUTPUT_HANDLE), Line.data(), static_cast<DWORD>(Line.size()),
            &Written, nullptr) && Written == Line.size();
    }

    ~FBridgeListener()
    {
        Io.stop();
        if (Worker.joinable())
        {
            Worker.join();
        }
    }

private:
    void Accept()
    {
        Acceptor.async_accept([this](Error Ec, Tcp::socket Socket)
        {
            if (Ec) { return; }
            Stream.emplace(std::move(Socket));
            Buffer.consume(Buffer.size());
            Parser.emplace();
            Parser->header_limit(4096);
            Parser->body_limit(16384);
            Stream->expires_after(std::chrono::seconds(2));
            Http::async_read_header(*Stream, Buffer, *Parser, [this](Error ReadError, size_t)
            {
                if (ReadError == Http::error::body_limit)
                {
                    Reply(Http::status::payload_too_large, R"({"error":"BODY_TOO_LARGE"})");
                }
                else if (ReadError)
                {
                    Close();
                }
                else
                {
                    Dispatch();
                }
            });
        });
    }

    void Dispatch()
    {
        const auto& Request = Parser->get();
        RefreshCommand();
        if (Request.version() != 11 || Request.count(Http::field::host) != 1
            || Request[Http::field::host] != Host || Request.count(Http::field::origin) != 0)
        {
            Reply(Http::status::forbidden, R"({"error":"FORBIDDEN_ORIGIN_OR_HOST"})");
        }
        else if (Request.count(Http::field::authorization) != 1
            || !MatchesAuthorization(Request[Http::field::authorization],
                Request.target() == "/qa/v1/session" || SessionAuthorization.empty()
                    ? BootstrapAuthorization : SessionAuthorization))
        {
            Reply(Http::status::unauthorized, R"({"error":"UNAUTHORIZED"})");
        }
        else if (Request.method() == Http::verb::post
            && (Request.target() == "/qa/v1/session" || Request.target() == "/qa/v1/session/heartbeat"
                || Request.target() == "/qa/v1/session/owner" || Request.target() == "/qa/v1/session/stop"
                || Request.target() == "/qa/v1/commands" || Request.target() == "/qa/v1/input/release-all"))
        {
            if (Parser->chunked() || !Parser->content_length().has_value() || Parser->is_done()
                || Request.count(Http::field::content_type) != 1
                || Request[Http::field::content_type] != "application/json")
            {
                Reply(Http::status::bad_request, R"({"error":"BAD_REQUEST"})");
                return;
            }
            Stream->expires_after(std::chrono::seconds(2));
            Http::async_read(*Stream, Buffer, *Parser, [this](Error Ec, size_t)
            {
                if (Ec) { Close(); }
                else { HandleSession(); }
            });
        }
        else if (!Parser->is_done())
        {
            Reply(Http::status::bad_request, R"({"error":"UNEXPECTED_BODY"})");
        }
        else if (Request.method() == Http::verb::get && Request.target() == "/qa/v1/health")
        {
            Reply(Http::status::ok, Input.Read().bReady
                ? R"({"mode":"UE","ready":true,"transport_ready":true})"
                : R"({"mode":"UE","ready":false,"transport_ready":true})");
        }
        else if (Request.method() == Http::verb::get && Request.target() == "/qa/v1/capabilities")
        {
            Reply(Http::status::ok, R"({"mode":"UE","schema_version":1,"input_enabled":true,"timeline":true,"jump_rise_event":true,"max_hold_ms":2000,"lease_ms":750})");
        }
        else if (Request.method() == Http::verb::get && Request.target() == "/qa/v1/mappings")
        {
            const auto Mappings = Input.ReadMappings();
            const auto& State = Mappings.State;
            auto Result = MakeShared<FJsonObject>();
            auto Keys = MakeShared<FJsonObject>();
            for (const auto& Binding : State.Bindings)
            {
                Keys->SetStringField(Binding.Key.ToString(), Binding.Action.ToString());
            }
            Result->SetNumberField(TEXT("mapping_revision"), State.Mapping);
            Result->SetObjectField(TEXT("keys"), Keys);
            auto Contexts = MakeShared<FJsonObject>();
            for (const auto& Item : Mappings.Contexts) { Contexts->SetNumberField(Item.Key, Item.Value); }
            Result->SetObjectField(TEXT("active_contexts"), Contexts);
            TArray<TSharedPtr<FJsonValue>> Effective;
            for (const auto& Mapping : Mappings.Effective)
            {
                auto Item = MakeShared<FJsonObject>();
                Item->SetStringField(TEXT("key"), Mapping.Key.ToString());
                Item->SetStringField(TEXT("action"), Mapping.Action);
                Item->SetNumberField(TEXT("value_type"), Mapping.ValueType);
                Item->SetArrayField(TEXT("triggers"), StringValues(Mapping.Triggers));
                Item->SetArrayField(TEXT("modifiers"), StringValues(Mapping.Modifiers));
                Effective.Add(MakeShared<FJsonValueObject>(Item));
            }
            Result->SetArrayField(TEXT("effective_mappings"), MoveTemp(Effective));
            ReplyJson(Result);
        }
        else if (Request.method() == Http::verb::get && Request.target() == "/qa/v1/observations" && !SessionAuthorization.empty())
        {
            const auto State = Input.Read();
            auto Result = Context();
            Result->SetBoolField(TEXT("player_exists"), State.bReady);
            Result->SetBoolField(TEXT("on_ground"), State.bGrounded);
            Result->SetBoolField(TEXT("lease_lost"), FPlatformTime::Seconds() >= LeaseUntil);
            Result->SetNumberField(TEXT("observation_seq"), State.EventSeq - SessionBaseSeq);
            Result->SetArrayField(TEXT("held_keys"), HeldKeys(State, State.Held));
            Result->SetArrayField(TEXT("observed_pressed_keys"), HeldKeys(State, State.Pressed));
            Result->SetArrayField(TEXT("pawn_position"), VectorValue(State.Position));
            Result->SetArrayField(TEXT("pawn_velocity"), VectorValue(State.Velocity));
            ReplyJson(Result);
        }
        else if (Request.method() == Http::verb::get && !SessionAuthorization.empty()
            && (Request.target() == "/qa/v1/events" || Request.target().starts_with("/qa/v1/events?after_seq=")))
        {
            uint64 Cursor = 0;
            if (Request.target() != "/qa/v1/events")
            {
                const auto Text = Request.target().substr(sizeof("/qa/v1/events?after_seq=") - 1);
                if (Text.empty() || Text.size() > 9
                    || !std::all_of(Text.begin(), Text.end(), [](char C) { return C >= '0' && C <= '9'; }))
                {
                    Reply(Http::status::bad_request, R"({"error":"INVALID_CURSOR"})");
                    return;
                }
                for (char C : Text) { Cursor = Cursor * 10 + C - '0'; }
            }
            bool bLost = false;
            TArray<TSharedPtr<FJsonValue>> Values;
            for (const auto& Edge : Input.ReadEvents(SessionBaseSeq + Cursor, bLost))
            {
                auto Value = MakeShared<FJsonObject>();
                Value->SetNumberField(TEXT("seq"), Edge.Seq - SessionBaseSeq);
                Value->SetStringField(TEXT("request_id"), Edge.Request.ToString(EGuidFormats::Digits).ToLower());
                Value->SetStringField(TEXT("layer"), Edge.Layer == FBridgeInput::ELayer::Pawn ? TEXT("L3")
                    : Edge.Layer == FBridgeInput::ELayer::Action ? TEXT("L2") : TEXT("L1"));
                Value->SetStringField(TEXT("kind"), Edge.Layer == FBridgeInput::ELayer::Pawn ? TEXT("JumpRise")
                    : Edge.Layer == FBridgeInput::ELayer::Action ? TEXT("Triggered") : Edge.bDown ? TEXT("KeyDown") : TEXT("KeyUp"));
                Value->SetStringField(TEXT("value"), Edge.Layer != FBridgeInput::ELayer::Key
                    ? Edge.Action.ToString()
                    : Edge.Key.ToString());
                Values.Add(MakeShared<FJsonValueObject>(Value));
            }
            auto Result = MakeShared<FJsonObject>();
            Result->SetArrayField(TEXT("events"), MoveTemp(Values));
            Result->SetBoolField(TEXT("lost"), bLost);
            ReplyJson(Result);
        }
        else if (Request.method() == Http::verb::get && Request.target().starts_with("/qa/v1/commands/")
            && !SessionAuthorization.empty())
        {
            const auto Text = Request.target().substr(16);
            const FUTF8ToTCHAR IdText(Text.data(), static_cast<int32>(Text.size()));
            FGuid Id;
            if (FGuid::Parse(FString(IdText.Length(), IdText.Get()), Id))
            {
                if (const auto* Record = Commands.Find(Id))
                {
                    ReplyStatus(Record->Status);
                    return;
                }
            }
            Reply(Http::status::not_found, R"({"error":"UNKNOWN_REQUEST"})");
        }
        else
        {
            Reply(Http::status::not_found, R"({"error":"UNKNOWN_ROUTE"})");
        }
    }

    static bool HasNumber(const FJsonObject& Json, const TCHAR* Name, double Expected)
    {
        const auto Value = Json.TryGetField(Name);
        return Value && Value->Type == EJson::Number && Value->AsNumber() == Expected;
    }

    static bool HasString(const FJsonObject& Json, const TCHAR* Name, const FString& Expected)
    {
        const auto Value = Json.TryGetField(Name);
        return Value && Value->Type == EJson::String && Value->AsString().Equals(Expected, ESearchCase::CaseSensitive);
    }

    static bool ValidOwner(const FString& Owner)
    {
        return Owner.Equals(TEXT("MANUAL"), ESearchCase::CaseSensitive)
            || Owner.Equals(TEXT("SCRIPTED"), ESearchCase::CaseSensitive)
            || Owner.Equals(TEXT("DECISIONS"), ESearchCase::CaseSensitive);
    }

    TSharedRef<FJsonObject> Context()
    {
        auto Context = MakeShared<FJsonObject>();
        Context->SetNumberField(TEXT("schema_version"), 1);
        Context->SetStringField(TEXT("session_id"), SessionId);
        Context->SetNumberField(TEXT("world_generation"), WorldGeneration);
        Context->SetNumberField(TEXT("expected_mapping_revision"), Input.Read().Mapping);
        Context->SetStringField(TEXT("input_owner"), InputOwner);
        Context->SetNumberField(TEXT("owner_revision"), OwnerRevision);
        return Context;
    }

    void ReplyJson(const TSharedRef<FJsonObject>& Value)
    {
        FString Json;
        FJsonSerializer::Serialize(Value, TJsonWriterFactory<>::Create(&Json));
        Reply(Http::status::ok, TCHAR_TO_UTF8(*Json));
    }

    void ReplyContext(const FString* Token = nullptr)
    {
        auto Result = Context();
        if (Token) { Result->SetStringField(TEXT("session_token"), *Token); }
        ReplyJson(Result);
    }

    static TArray<TSharedPtr<FJsonValue>> HeldKeys(const FBridgeInput::FState& State, FBridgeInput::FInputMask Held)
    {
        TArray<TSharedPtr<FJsonValue>> Keys;
        for (int32 Index = 0; Index < State.Bindings.Num(); ++Index)
        {
            if (Held & FBridgeInput::InputBit(Index))
            {
                Keys.Add(MakeShared<FJsonValueString>(State.Bindings[Index].Key.ToString()));
            }
        }
        return Keys;
    }

    static TArray<TSharedPtr<FJsonValue>> VectorValue(FVector Value)
    {
        return {MakeShared<FJsonValueNumber>(Value.X), MakeShared<FJsonValueNumber>(Value.Y),
            MakeShared<FJsonValueNumber>(Value.Z)};
    }

    static TArray<TSharedPtr<FJsonValue>> StringValues(const TArray<FString>& Values)
    {
        TArray<TSharedPtr<FJsonValue>> Result;
        for (const auto& Value : Values) { Result.Add(MakeShared<FJsonValueString>(Value)); }
        return Result;
    }

    struct FCommand
    {
        TSharedPtr<FJsonObject> Payload;
        FBridgeInput::EStatus Status = FBridgeInput::EStatus::Running;
    };

    void RefreshCommand()
    {
        if (auto* Record = Commands.Find(ActiveRequest))
        {
            Record->Status = Input.Read().Status;
            if (Record->Status != FBridgeInput::EStatus::Running)
            {
                ActiveRequest.Invalidate();
            }
        }
    }

    void ReplyStatus(FBridgeInput::EStatus Status)
    {
        Reply(Http::status::ok, Status == FBridgeInput::EStatus::Running ? R"({"status":"running"})"
            : Status == FBridgeInput::EStatus::Completed ? R"({"status":"completed"})"
            : R"({"status":"cancelled"})");
    }

    static int32 ReadInteger(const FJsonValue* Value, int32 Min, int32 Max)
    {
        if (!Value || Value->Type != EJson::Number) { return -1; }
        const double N = Value->AsNumber();
        return N >= Min && N <= Max && FMath::FloorToDouble(N) == N ? static_cast<int32>(N) : -1;
    }

    static int32 ResolveKey(const FJsonValue* Value, const FBridgeInput::FState& State)
    {
        if (!Value || Value->Type != EJson::String) { return INDEX_NONE; }
        const FString Name = Value->AsString();
        return State.Bindings.IndexOfByPredicate([&Name](const FBridgeInput::FBinding& Binding)
        {
            return Name.Equals(Binding.Key.ToString(), ESearchCase::CaseSensitive);
        });
    }

    static bool ReadPlan(const FJsonObject& Json, const FBridgeInput::FState& State,
        TArray<FBridgeInput::FTransition>& Plan, int32& Duration)
    {
        if (Json.HasField(TEXT("steps"))) { return false; }
        const TArray<TSharedPtr<FJsonValue>>* Entries = nullptr;
        if (Json.HasField(TEXT("timeline")))
        {
            Duration = ReadInteger(Json.TryGetField(TEXT("duration_ms")).Get(), 0, 3000);
            if (Duration < 0 || Json.HasField(TEXT("keys")) || Json.HasField(TEXT("hold_ms"))
                || !Json.TryGetArrayField(TEXT("timeline"), Entries) || Entries->Num() > 96) { return false; }
            int32 Previous = 0;
            TArray<int32> Started;
            Started.Init(-1, State.Bindings.Num());
            for (const auto& Entry : *Entries)
            {
                if (Entry->Type != EJson::Array || Entry->AsArray().Num() != 3) { return false; }
                const auto& Fields = Entry->AsArray();
                const int32 At = ReadInteger(Fields[0].Get(), Previous, Duration);
                const int32 Key = ResolveKey(Fields[2].Get(), State);
                if (At < 0 || Key == INDEX_NONE || Fields[1]->Type != EJson::String) { return false; }
                const FString Kind = Fields[1]->AsString();
                const bool bDown = Kind.Equals(TEXT("down"), ESearchCase::CaseSensitive);
                if (!bDown && !Kind.Equals(TEXT("up"), ESearchCase::CaseSensitive)) { return false; }
                int32& Start = Started[Key];
                if (bDown ? Start >= 0 : (Start < 0 || At - Start < 20 || At - Start > 2000)) { return false; }
                Start = bDown ? At : -1;
                Previous = At;
                Plan.Add({At, Key, bDown});
            }
            return !Started.ContainsByPredicate([](int32 Start) { return Start >= 0; });
        }
        Duration = ReadInteger(Json.TryGetField(TEXT("hold_ms")).Get(), 20, 2000);
        if (Duration < 0 || Json.HasField(TEXT("duration_ms")) || !Json.TryGetArrayField(TEXT("keys"), Entries)
            || Entries->IsEmpty() || Entries->Num() > FBridgeInput::MaxInputs) { return false; }
        FBridgeInput::FInputMask Mask = 0;
        for (const auto& Entry : *Entries)
        {
            const int32 Key = ResolveKey(Entry.Get(), State);
            if (Key == INDEX_NONE) { return false; }
            const auto Bit = FBridgeInput::InputBit(Key);
            if (Mask & Bit) { return false; }
            Plan.Add({0, Key, true});
            Mask |= Bit;
        }
        const int32 Count = Plan.Num();
        for (int32 Index = 0; Index < Count; ++Index) { Plan.Add({Duration, Plan[Index].BindingIndex, false}); }
        return true;
    }

    void BeginCommand(const TSharedPtr<FJsonObject>& Json, FGuid Id, int32 DeadlineMs, double Now)
    {
        if (const auto* Prior = Commands.Find(Id))
        {
            if (FJsonValueObject(Json) == FJsonValueObject(Prior->Payload)) { ReplyStatus(Prior->Status); }
            else { Reply(Http::status::conflict, R"({"error":"IDEMPOTENCY_CONFLICT"})"); }
            return;
        }
        if (ActiveRequest.IsValid() || Commands.Num() >= 1000)
        {
            Reply(Http::status::conflict, ActiveRequest.IsValid() ? R"({"error":"INPUT_BUSY"})"
                : R"({"error":"SESSION_COMMAND_LIMIT"})");
            return;
        }
        if (Json->HasField(TEXT("expected_observation_seq"))
            && !HasNumber(*Json, TEXT("expected_observation_seq"), Input.Read().EventSeq - SessionBaseSeq))
        {
            Reply(Http::status::conflict, R"({"error":"STALE_OBSERVATION"})");
            return;
        }
        const auto State = Input.Read();
        TArray<FBridgeInput::FTransition> Plan;
        int32 Duration = 0;
        if (!ReadPlan(*Json, State, Plan, Duration))
        {
            Reply(Http::status::bad_request, R"({"error":"INVALID_INPUT"})");
            return;
        }
        if (Duration > DeadlineMs)
        {
            Reply(Http::status::bad_request, R"({"error":"DEADLINE_TOO_SHORT"})");
            return;
        }
        if (!HasNumber(*Json, TEXT("expected_mapping_revision"), State.Mapping)
            || !Input.Begin(Id, MoveTemp(Plan), Duration, DeadlineMs,
                WorldGeneration, State.Mapping, Now + LeaseMs / 1000.0))
        {
            Reply(Http::status::conflict, R"({"error":"INPUT_NOT_READY"})");
            return;
        }
        LeaseUntil = Now + LeaseMs / 1000.0;
        ActiveRequest = Id;
        Commands.Add(Id, {Json});
        ReplyStatus(FBridgeInput::EStatus::Running);
    }

    void HandleSession()
    {
        const auto& Request = Parser->get();
        const auto& Body = Request.body();
        const FUTF8ToTCHAR Utf8(Body.data(), static_cast<int32>(Body.size()));
        const FString Text(Utf8.Length(), Utf8.Get());
        TSharedPtr<FJsonObject> Json;
        if (!FJsonSerializer::Deserialize(TJsonReaderFactory<>::Create(Text), Json) || !Json)
        {
            Reply(Http::status::bad_request, R"({"error":"BAD_REQUEST"})");
            return;
        }
        if (!HasNumber(*Json, TEXT("schema_version"), 1))
        {
            Reply(Http::status::bad_request, R"({"error":"SCHEMA_MISMATCH"})");
            return;
        }
        const auto Route = Request.target();
        const double Now = FPlatformTime::Seconds();
        const auto World = Input.Read();
        if (Route == "/qa/v1/session")
        {
            FString Owner = TEXT("MANUAL");
            if (const auto Value = Json->TryGetField(TEXT("input_owner")))
            {
                Owner = Value->Type == EJson::String ? Value->AsString() : FString();
            }
            if (!ValidOwner(Owner))
            {
                Reply(Http::status::bad_request, R"({"error":"INVALID_INPUT_OWNER"})");
            }
            else if (!SessionAuthorization.empty() && Now < LeaseUntil)
            {
                Reply(Http::status::conflict, R"({"error":"SESSION_BUSY"})");
            }
            else if (!World.bReady || !Input.Cancel())
            {
                Reply(Http::status::conflict, R"({"error":"INPUT_NOT_READY"})");
            }
            else
            {
                uint8 Random[32];
                if (BCryptGenRandom(nullptr, Random, sizeof Random, BCRYPT_USE_SYSTEM_PREFERRED_RNG) != 0)
                {
                    Reply(Http::status::internal_server_error, R"({"error":"SESSION_TOKEN_ERROR"})");
                    return;
                }
                const FString Token = BytesToHex(Random, sizeof Random);
                SessionAuthorization = std::string("Bearer ") + TCHAR_TO_UTF8(*Token);
                SessionId = FGuid::NewGuid().ToString(EGuidFormats::Digits);
                WorldGeneration = World.World;
                InputOwner = MoveTemp(Owner);
                OwnerRevision = 1;
                Commands.Reset();
                ActiveRequest.Invalidate();
                SessionBaseSeq = Input.Read().EventSeq;
                LeaseUntil = Now + LeaseMs / 1000.0;
                ReplyContext(&Token);
            }
            return;
        }

        FString RequestId;
        FGuid ParsedId;
        if (!Json->TryGetStringField(TEXT("request_id"), RequestId) || !FGuid::Parse(RequestId, ParsedId))
        {
            Reply(Http::status::bad_request, R"({"error":"INVALID_REQUEST_ID"})");
            return;
        }
        const auto Deadline = Json->TryGetField(TEXT("deadline_ms"));
        if (!Deadline || Deadline->Type != EJson::Number || Deadline->AsNumber() < 1
            || Deadline->AsNumber() > 3000 || FMath::FloorToDouble(Deadline->AsNumber()) != Deadline->AsNumber())
        {
            Reply(Http::status::bad_request, R"({"error":"INVALID_DEADLINE"})");
            return;
        }
        if (SessionAuthorization.empty())
        {
            Reply(Http::status::bad_request, R"({"error":"NO_SESSION"})");
        }
        else if (!HasString(*Json, TEXT("session_id"), SessionId))
        {
            Reply(Http::status::bad_request, R"({"error":"SESSION_MISMATCH"})");
        }
        else if (!HasNumber(*Json, TEXT("world_generation"), WorldGeneration) || World.World != WorldGeneration)
        {
            Reply(Http::status::bad_request, R"({"error":"STALE_WORLD"})");
        }
        else if (!HasNumber(*Json, TEXT("expected_mapping_revision"), World.Mapping))
        {
            Reply(Http::status::bad_request, R"({"error":"MAPPING_CHANGED"})");
        }
        else if (!HasString(*Json, TEXT("input_owner"), InputOwner))
        {
            Reply(Http::status::bad_request, R"({"error":"INPUT_OWNER_MISMATCH"})");
        }
        else if (!HasNumber(*Json, TEXT("owner_revision"), OwnerRevision))
        {
            Reply(Http::status::bad_request, R"({"error":"STALE_INPUT_OWNER"})");
        }
        else if (Route == "/qa/v1/session/stop")
        {
            if (!Input.Cancel())
            {
                Reply(Http::status::conflict, R"({"error":"KEY_RELEASE_PENDING"})");
                return;
            }
            SessionAuthorization.clear();
            SessionId.Reset();
            Reply(Http::status::ok, R"({"held_keys":[]})");
        }
        else if (Route == "/qa/v1/input/release-all")
        {
            if (Input.Cancel()) { Reply(Http::status::ok, R"({"held_keys":[]})"); }
            else { Reply(Http::status::conflict, R"({"error":"KEY_RELEASE_PENDING"})"); }
        }
        else if (Now >= LeaseUntil && !(Route == "/qa/v1/commands" && Commands.Contains(ParsedId)))
        {
            Reply(Http::status::conflict, R"({"error":"LEASE_EXPIRED"})");
        }
        else if (Route == "/qa/v1/commands")
        {
            BeginCommand(Json, ParsedId, static_cast<int32>(Deadline->AsNumber()), Now);
        }
        else if (Route == "/qa/v1/session/heartbeat")
        {
            LeaseUntil = Now + LeaseMs / 1000.0;
            Input.Renew(LeaseUntil);
            Reply(Http::status::ok, R"({"lease_ms":750})");
        }
        else
        {
            FString Next;
            if (!Json->TryGetStringField(TEXT("next_owner"), Next) || !ValidOwner(Next))
            {
                Reply(Http::status::bad_request, R"({"error":"INVALID_INPUT_OWNER"})");
                return;
            }
            if (Next != InputOwner)
            {
                if (!Input.Cancel())
                {
                    Reply(Http::status::conflict, R"({"error":"KEY_RELEASE_PENDING"})");
                    return;
                }
                InputOwner = MoveTemp(Next);
                ++OwnerRevision;
            }
            ReplyContext();
        }
    }

    void Reply(Http::status Status, const char* Body)
    {
        Response = {Status, 11};
        Response.set(Http::field::content_type, "application/json");
        Response.set(Http::field::cache_control, "no-store");
        Response.keep_alive(false);
        Response.body() = Body;
        Response.prepare_payload();
        Stream->expires_after(std::chrono::seconds(2));
        Http::async_write(*Stream, Response, [this](Error, size_t) { Close(); });
    }

    void Close()
    {
        Error Ec;
        Stream->socket().shutdown(Tcp::socket::shutdown_send, Ec);
        Stream.reset();
        Accept();
    }

    static bool MatchesAuthorization(Beast::string_view Value, const std::string& Authorization)
    {
        if (Value.size() != Authorization.size()) { return false; }
        unsigned Difference = 0;
        for (size_t Index = 0; Index < Value.size(); ++Index)
        {
            Difference |= static_cast<unsigned>(Value[Index] ^ Authorization[Index]);
        }
        return Difference == 0;
    }

    const std::string BootstrapAuthorization;
    FBridgeInput& Input;
    std::string SessionAuthorization;
    FString SessionId;
    FString InputOwner;
    double LeaseUntil = 0.0;
    int32 WorldGeneration = 0;
    int32 OwnerRevision = 0;
    uint64 SessionBaseSeq = 0;
    FGuid ActiveRequest;
    TMap<FGuid, FCommand> Commands;
    std::string Host;
    Asio::io_context Io;
    Tcp::acceptor Acceptor{Io};
    std::optional<Beast::tcp_stream> Stream;
    Beast::flat_buffer Buffer{4096};
    std::optional<Http::request_parser<Http::string_body>> Parser;
    Http::response<Http::string_body> Response;
    std::thread Worker;
};
}

class FGameQABridge : public IModuleInterface
{
    void StartupModule() override
    {
        const FString Token = FPlatformMisc::GetEnvironmentVariable(TEXT("UNREALAUTOQA_BRIDGE_BOOTSTRAP"));
        FPlatformMisc::SetEnvironmentVar(TEXT("UNREALAUTOQA_BRIDGE_BOOTSTRAP"), nullptr);
        if (Token.Len() != 64) { return; }
        for (const TCHAR Character : Token)
        {
            if (!FChar::IsHexDigit(Character)) { return; }
        }

        Input.Configure();
        Listener = MakeUnique<FBridgeListener>(Token, Input);
        if (!Listener->Start())
        {
            Listener.Reset();
        }
        else
        {
            TickHandle = FWorldDelegates::OnWorldTickStart.AddRaw(&Input, &FBridgeInput::Tick);
            WatchdogHandle = FTSTicker::GetCoreTicker().AddTicker(
                FTickerDelegate::CreateRaw(&Input, &FBridgeInput::Watchdog));
            PostTickHandle = FWorldDelegates::OnWorldPostActorTick.AddRaw(&Input, &FBridgeInput::PostTick);
            CleanupHandle = FWorldDelegates::OnWorldCleanup.AddLambda(
                [this](UWorld* World, bool, bool) { Input.Cleanup(World); });
        }
    }

    void ShutdownModule() override
    {
        Listener.Reset();
        FTSTicker::GetCoreTicker().RemoveTicker(WatchdogHandle);
        FWorldDelegates::OnWorldTickStart.Remove(TickHandle);
        FWorldDelegates::OnWorldPostActorTick.Remove(PostTickHandle);
        FWorldDelegates::OnWorldCleanup.Remove(CleanupHandle);
        Input.Cleanup(nullptr);
    }

    FBridgeInput Input;
    FDelegateHandle TickHandle;
    FDelegateHandle PostTickHandle;
    FTSTicker::FDelegateHandle WatchdogHandle;
    FDelegateHandle CleanupHandle;
    TUniquePtr<FBridgeListener> Listener;
};

IMPLEMENT_MODULE(FGameQABridge, GameQABridge)
#else
IMPLEMENT_MODULE(FDefaultModuleImpl, GameQABridge)
#endif
