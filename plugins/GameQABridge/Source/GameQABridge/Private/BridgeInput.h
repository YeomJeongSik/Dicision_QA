#pragma once

#include "Engine/GameViewportClient.h"
#include "Engine/LocalPlayer.h"
#include "Engine/World.h"
#include "EnhancedActionKeyMapping.h"
#include "EnhancedInputSubsystems.h"
#include "EnhancedPlayerInput.h"
#include "GameFramework/Character.h"
#include "GameFramework/CharacterMovementComponent.h"
#include "GameFramework/PlayerController.h"
#include "GenericPlatform/GenericPlatformInputDeviceMapper.h"
#include "HAL/PlatformProcess.h"
#include "HAL/PlatformTime.h"
#include "InputKeyEventArgs.h"
#include "InputAction.h"
#include "InputMappingContext.h"
#include "InputModifiers.h"
#include "InputTriggers.h"
#include "Misc/Guid.h"
#include "Misc/ConfigCacheIni.h"
#include "UObject/UObjectIterator.h"
#include <array>
#include <mutex>

class FBridgeInput
{
public:
    enum class EStatus { Idle, Running, Completed, Cancelled };
    enum class ELayer { Key, Action, Pawn };

    struct FTransition
    {
        int32 AtMs;
        uint8 Bit;
        bool bDown;
    };

    struct FEdge
    {
        uint64 Seq = 0;
        FGuid Request;
        uint8 Bit = 0;
        FName Key;
        bool bDown = false;
        ELayer Layer = ELayer::Key;
    };
    static constexpr size_t EventLimit = 256;

    struct FState
    {
        EStatus Status = EStatus::Idle;
        int32 World = 0;
        int32 Mapping = 0;
        uint8 Allowed = 0;
        uint8 Held = 0;
        uint8 Pressed = 0;
        FName MoveKey;
        FName JumpKey;
        uint64 EventSeq = 0;
        bool bReady = false;
        bool bGrounded = false;
        FVector Position = FVector::ZeroVector;
        FVector Velocity = FVector::ZeroVector;
    };

    struct FMappingInfo
    {
        FName Key;
        FString Action;
        int32 ValueType = 0;
        TArray<FString> Triggers;
        TArray<FString> Modifiers;
    };

    struct FMappings
    {
        FState State;
        TArray<FMappingInfo> Effective;
        TMap<FString, int32> Contexts;
    };

    void Configure()
    {
        TArray<FString> Configured;
        if (!GConfig->GetArray(TEXT("GameQABridge"), TEXT("ApprovedKeys"), Configured, GGameIni))
        {
            Configured = {TEXT("W"), TEXT("SpaceBar")};
        }
        for (const FString& Name : Configured)
        {
            const FKey Key(FName(*Name, FNAME_Find));
            if (Key.IsValid() && Key.IsBindableInBlueprints() && !Key.IsGamepadKey()
                && !Key.IsMouseButton() && !Key.IsAnalog() && !Key.IsTouch())
            {
                ApprovedKeys.Add(Key);
            }
        }
    }

    FState Read()
    {
        const std::lock_guard Lock(Mutex);
        return State;
    }

    FMappings ReadMappings()
    {
        const std::lock_guard Lock(Mutex);
        return {State, EffectiveMappings, ActiveContexts};
    }

    TArray<FEdge> ReadEvents(uint64 After, bool& bLost)
    {
        const std::lock_guard Lock(Mutex);
        const uint64 First = State.EventSeq > EventLimit ? State.EventSeq - EventLimit : 0;
        bLost = After < First;
        TArray<FEdge> Result;
        for (uint64 Seq = FMath::Max(After, First); Seq < State.EventSeq; ++Seq)
        {
            Result.Add(Events[Seq % EventLimit]);
        }
        return Result;
    }

    bool Begin(FGuid Id, TArray<FTransition> Plan, int32 DurationMs, int32 DeadlineMs,
        int32 World, int32 Mapping, double Lease)
    {
        const std::lock_guard Lock(Mutex);
        if (!State.bReady || State.Status == EStatus::Running || State.World != World
            || State.Mapping != Mapping)
        {
            return false;
        }
        State.Status = EStatus::Running;
        Request = Id;
        Transitions = MoveTemp(Plan);
        Duration = DurationMs;
        Next = 0;
        NextAt = 0.0;
        const double Now = FPlatformTime::Seconds();
        DeadlineAt = Now + DeadlineMs / 1000.0;
        LeaseUntil = Lease;
        bCancel = false;
        return true;
    }

    void Renew(double Until)
    {
        const std::lock_guard Lock(Mutex);
        LeaseUntil = Until;
    }

    bool Watchdog(float)
    {
        const std::lock_guard Lock(Mutex);
        Expire(FPlatformTime::Seconds());
        return true;
    }

    bool Cancel()
    {
        {
            const std::lock_guard Lock(Mutex);
            if (State.Status != EStatus::Running) { return true; }
            bCancel = true;
        }
        // Only the HTTP worker waits. The tick acknowledges after dispatching key releases.
        const double Limit = FPlatformTime::Seconds() + 0.3;
        while (FPlatformTime::Seconds() < Limit)
        {
            if (Read().Status != EStatus::Running) { return true; }
            FPlatformProcess::SleepNoStats(0.001f);
        }
        return false;
    }

    void Cleanup(UWorld* World)
    {
        const std::lock_guard Lock(Mutex);
        if (!World || TargetWorld.Get() == World)
        {
            Finish(EStatus::Cancelled);
            TargetWorld.Reset();
            TargetController.Reset();
            TargetPawn.Reset();
            MoveAction.Reset();
            JumpAction.Reset();
            State.bReady = false;
            State.Allowed = 0;
            State.Pressed = 0;
            State.MoveKey = State.JumpKey = NAME_None;
            EffectiveMappings.Reset();
            ActiveContexts.Reset();
            MappingSignature = 0;
            ReadyFrames = 0;
        }
    }

    void Tick(UWorld* World, ELevelTick, float)
    {
        if (!World->IsGameWorld()) { return; }
        const std::lock_guard Lock(Mutex);
        const double Now = FPlatformTime::Seconds();
        Expire(Now);
        LastWorldTick = Now;
        APlayerController* PC = World->GetFirstPlayerController();
        ULocalPlayer* LP = PC ? PC->GetLocalPlayer() : nullptr;
        auto* PlayerInput = PC ? Cast<UEnhancedPlayerInput>(PC->PlayerInput) : nullptr;
        APawn* Pawn = PC ? PC->GetPawn() : nullptr;
        UGameViewportClient* Viewport = LP ? LP->ViewportClient : nullptr;
        if (TargetWorld.Get() != World || TargetController.Get() != PC || TargetPawn.Get() != Pawn)
        {
            Finish(EStatus::Cancelled);
            TargetWorld = World;
            TargetController = PC;
            TargetPawn = Pawn;
            ++State.World;
            ReadyFrames = 0;
        }
        const auto Mapping = FindAllowed(PlayerInput);
        if (Mapping.Signature != MappingSignature || Mapping.Move != MoveAction || Mapping.Jump != JumpAction
            || Mapping.MoveKey.GetFName() != State.MoveKey || Mapping.JumpKey.GetFName() != State.JumpKey)
        {
            Finish(EStatus::Cancelled);
            MoveAction = Mapping.Move;
            JumpAction = Mapping.Jump;
            State.MoveKey = Mapping.MoveKey.GetFName();
            State.JumpKey = Mapping.JumpKey.GetFName();
            State.Allowed = (MoveAction.IsValid() ? 1 : 0) | (JumpAction.IsValid() ? 2 : 0);
            MappingSignature = Mapping.Signature;
            RefreshMappings(LP, PlayerInput);
            ++State.Mapping;
        }
        if (Pawn && Viewport && Viewport->Viewport && State.Allowed)
        {
            // Startup can stall on the first rendered frame after the player has spawned.
            ReadyFrames = FMath::Min(ReadyFrames + 1, 8);
        }
        else
        {
            ReadyFrames = 0;
        }
        State.bReady = ReadyFrames == 8;
        State.Pressed = PC ? ((State.Allowed & 1) && PC->IsInputKeyDown(FKey(State.MoveKey)) ? 1 : 0)
            | ((State.Allowed & 2) && PC->IsInputKeyDown(FKey(State.JumpKey)) ? 2 : 0) : 0;
        if (Pawn)
        {
            State.Position = Pawn->GetActorLocation();
            State.Velocity = Pawn->GetVelocity();
            const auto* Character = Cast<ACharacter>(Pawn);
            State.bGrounded = Character && Character->GetCharacterMovement()->IsMovingOnGround();
        }
        if (State.Status != EStatus::Running) { return; }

        if (!State.bReady || World->IsPaused())
        {
            Finish(EStatus::Cancelled);
            return;
        }
        if (NextAt == 0.0)
        {
            NextAt = Now + (Transitions.IsEmpty() ? Duration : Transitions[0].AtMs) / 1000.0;
        }
        if (Now < NextAt) { return; }
        if (!Transitions.IsValidIndex(Next))
        {
            Finish(EStatus::Completed);
            return;
        }

        DownDevice = IPlatformInputDeviceMapper::Get().GetPrimaryInputDeviceForUser(LP->GetPlatformUserId());
        DownViewport = Viewport;
        const int32 At = Transitions[Next].AtMs;
        uint8 Changed = 0;
        while (Transitions.IsValidIndex(Next) && Transitions[Next].AtMs == At)
        {
            const auto& Step = Transitions[Next];
            if (Changed & Step.Bit) { break; }
            if (!DownDevice.IsValid() || (Step.bDown && (State.Pressed & Step.Bit)))
            {
                Finish(EStatus::Cancelled);
                return;
            }
            if (Step.bDown) { State.Held |= Step.Bit; }
            if (!Send(Step.Bit, Step.bDown))
            {
                Finish(EStatus::Cancelled);
                return;
            }
            if (!Step.bDown) { State.Held &= ~Step.Bit; }
            ReportedActions &= ~Step.Bit;
            Record(Step.Bit, Step.bDown);
            Changed |= Step.Bit;
            ++Next;
        }
        const int32 Following = Transitions.IsValidIndex(Next) ? Transitions[Next].AtMs : Duration;
        NextAt = Now + (Following - At) / 1000.0;
    }

    void PostTick(UWorld* World, ELevelTick, float)
    {
        const std::lock_guard Lock(Mutex);
        if (World != TargetWorld.Get() || State.Status != EStatus::Running) { return; }
        const auto* PC = TargetController.Get();
        const auto* PlayerInput = PC ? Cast<UEnhancedPlayerInput>(PC->PlayerInput) : nullptr;
        if (!PlayerInput) { return; }
        for (uint8 Bit : {uint8{1}, uint8{2}})
        {
            if (!(State.Held & Bit) || (ReportedActions & Bit)) { continue; }
            const auto* Action = Bit == 1 ? MoveAction.Get() : JumpAction.Get();
            const auto* Instance = Action ? PlayerInput->FindActionInstanceData(Action) : nullptr;
            if (Instance && Instance->GetTriggerEvent() == ETriggerEvent::Triggered)
            {
                Record(Bit, true, ELayer::Action);
                ReportedActions |= Bit;
                if (Bit == 2 && State.bGrounded)
                {
                    JumpStart = State.Position.Z;
                    bObserveJump = true;
                }
            }
        }
        const auto* Pawn = TargetPawn.Get();
        if (bObserveJump && Pawn && Pawn->GetActorLocation().Z > JumpStart + 0.5 && Pawn->GetVelocity().Z > 0.0)
        {
            Record(2, true, ELayer::Pawn);
            bObserveJump = false;
        }
    }

private:
    struct FAllowedActions
    {
        TWeakObjectPtr<const UInputAction> Move;
        TWeakObjectPtr<const UInputAction> Jump;
        FKey MoveKey;
        FKey JumpKey;
        uint32 Signature = 0;
    };

    FAllowedActions FindAllowed(const UEnhancedPlayerInput* Input) const
    {
        FAllowedActions Keys;
        static const FName MoveName(TEXT("IA_Move"));
        static const FName JumpName(TEXT("IA_Jump"));
        bool bAmbiguousMove = false;
        bool bAmbiguousJump = false;
        const auto Select = [](const FEnhancedActionKeyMapping& Mapping,
            TWeakObjectPtr<const UInputAction>& Action, FKey& Key, bool& bAmbiguous)
        {
            bAmbiguous |= Action.IsValid() && (Action.Get() != Mapping.Action.Get() || Key != Mapping.Key);
            Action = Mapping.Action.Get();
            Key = Mapping.Key;
        };
        if (Input)
        {
            for (const auto& Mapping : Input->GetEnhancedActionMappingsView())
            {
                if (!Mapping.Action) { continue; }
                Keys.Signature = HashCombine(Keys.Signature, GetTypeHash(Mapping.Key.GetFName()));
                Keys.Signature = HashCombine(Keys.Signature, PointerHash(Mapping.Action.Get()));
                for (const auto& Trigger : Mapping.Triggers) { Keys.Signature = HashCombine(Keys.Signature, PointerHash(Trigger.Get())); }
                for (const auto& Modifier : Mapping.Modifiers) { Keys.Signature = HashCombine(Keys.Signature, PointerHash(Modifier.Get())); }
                if (!ApprovedKeys.Contains(Mapping.Key)) { continue; }
                const FName Name = Mapping.Action->GetFName();
                if (Name == MoveName) { Select(Mapping, Keys.Move, Keys.MoveKey, bAmbiguousMove); }
                if (Name == JumpName) { Select(Mapping, Keys.Jump, Keys.JumpKey, bAmbiguousJump); }
            }
        }
        if (Keys.MoveKey.IsValid() && Keys.MoveKey == Keys.JumpKey) { bAmbiguousMove = bAmbiguousJump = true; }
        if (bAmbiguousMove) { Keys.Move.Reset(); Keys.MoveKey = FKey(); }
        if (bAmbiguousJump) { Keys.Jump.Reset(); Keys.JumpKey = FKey(); }
        return Keys;
    }

    void RefreshMappings(ULocalPlayer* Player, const UEnhancedPlayerInput* Input)
    {
        EffectiveMappings.Reset();
        ActiveContexts.Reset();
        if (const auto* Subsystem = Player ? ULocalPlayer::GetSubsystem<UEnhancedInputLocalPlayerSubsystem>(Player) : nullptr)
        {
            for (TObjectIterator<UInputMappingContext> Context; Context; ++Context)
            {
                int32 Priority = 0;
                if (Subsystem->HasMappingContext(*Context, Priority))
                {
                    ActiveContexts.Add(Context->GetPathName(), Priority);
                }
            }
        }
        if (!Input) { return; }
        for (const auto& Mapping : Input->GetEnhancedActionMappingsView())
        {
            if (!Mapping.Action) { continue; }
            auto& Info = EffectiveMappings.AddDefaulted_GetRef();
            Info.Key = Mapping.Key.GetFName();
            Info.Action = Mapping.Action->GetPathName();
            Info.ValueType = static_cast<int32>(Mapping.Action->ValueType);
            for (const auto& Item : Mapping.Triggers) { if (Item) { Info.Triggers.Add(Item->GetClass()->GetName()); } }
            for (const auto& Item : Mapping.Modifiers) { if (Item) { Info.Modifiers.Add(Item->GetClass()->GetName()); } }
        }
    }

    bool Send(uint8 Bit, bool bPress)
    {
        auto* Viewport = DownViewport.Get();
        auto* PC = TargetController.Get();
        const auto Event = FInputKeyEventArgs::CreateSimulated(FKey(Bit == 1 ? State.MoveKey : State.JumpKey),
            bPress ? IE_Pressed : IE_Released, bPress ? 1.0f : 0.0f,
            -1, DownDevice, false, Viewport ? Viewport->Viewport : nullptr);
        if (Viewport && Viewport->Viewport && PC && TargetWorld.IsValid() && PC == TargetWorld->GetFirstPlayerController()
            && Viewport->InputKey(Event)) { return true; }
        return !bPress && PC && PC->InputKey(Event);
    }

    void Finish(EStatus Status)
    {
        if (State.Status != EStatus::Running) { return; }
        for (uint8 Bit : {uint8{1}, uint8{2}})
        {
            if (State.Held & Bit)
            {
                Send(Bit, false);
                Record(Bit, false);
            }
        }
        State.Held = 0;
        Transitions.Reset();
        ReportedActions = 0;
        bObserveJump = false;
        State.Status = Status;
    }

    void Expire(double Now)
    {
        if (State.Status == EStatus::Running
            && (bCancel || Now >= LeaseUntil || Now >= DeadlineAt || Now - LastWorldTick >= 0.75))
        {
            Finish(EStatus::Cancelled);
        }
    }

    void Record(uint8 Bit, bool bDown, ELayer Layer = ELayer::Key)
    {
        const uint64 Index = State.EventSeq++;
        Events[Index % EventLimit] = {State.EventSeq, Request, Bit, Bit == 1 ? State.MoveKey : State.JumpKey, bDown, Layer};
    }

    std::mutex Mutex;
    FState State;
    TSet<FKey> ApprovedKeys;
    TArray<FMappingInfo> EffectiveMappings;
    TMap<FString, int32> ActiveContexts;
    uint32 MappingSignature = 0;
    std::array<FEdge, EventLimit> Events;
    FGuid Request;
    TWeakObjectPtr<UWorld> TargetWorld;
    TWeakObjectPtr<APlayerController> TargetController;
    TWeakObjectPtr<APawn> TargetPawn;
    TWeakObjectPtr<const UInputAction> MoveAction;
    TWeakObjectPtr<const UInputAction> JumpAction;
    TWeakObjectPtr<UGameViewportClient> DownViewport;
    FInputDeviceId DownDevice = INPUTDEVICEID_NONE;
    TArray<FTransition> Transitions;
    int32 Next = 0, Duration = 0;
    int32 ReadyFrames = 0;
    uint8 ReportedActions = 0;
    double NextAt = 0.0, DeadlineAt = 0.0, LeaseUntil = 0.0, LastWorldTick = 0.0, JumpStart = 0.0;
    bool bCancel = false;
    bool bObserveJump = false;
};
