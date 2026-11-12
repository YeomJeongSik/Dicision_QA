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

    static constexpr int32 MaxInputs = 12;

    struct FBinding
    {
        FName Key;
        FName Action;
        bool operator==(const FBinding&) const = default;
    };

    struct FTransition
    {
        int32 AtMs;
        int32 BindingIndex;
        bool bDown;
    };

    struct FEdge
    {
        uint64 Seq = 0;
        FGuid Request;
        FName Key;
        FName Action;
        bool bDown = false;
        ELayer Layer = ELayer::Key;
    };
    static constexpr size_t EventLimit = 256;

    struct FState
    {
        EStatus Status = EStatus::Idle;
        int32 World = 0;
        int32 Mapping = 0;
        TSet<int32> Held;
        TSet<int32> Pressed;
        TArray<FBinding> Bindings;
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
        GConfig->GetBool(TEXT("GameQABridge"), TEXT("AutoDiscoverKeys"), bAutoDiscoverKeys, GGameIni);
        TArray<FString> Configured;
        if (!GConfig->GetArray(TEXT("GameQABridge"), TEXT("ApprovedKeys"), Configured, GGameIni))
        {
            Configured = {TEXT("W"), TEXT("SpaceBar")};
        }
        for (const FString& Name : Configured)
        {
            const FKey Key(FName(*Name, FNAME_Find));
            if (IsDigitalInput(Key))
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
            Actions.Reset();
            State.bReady = false;
            State.Pressed.Reset();
            State.Bindings.Reset();
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
        auto Mapping = FindAllowed(PlayerInput);
        if (Mapping.Signature != MappingSignature || Mapping.Bindings != State.Bindings || Mapping.Actions != Actions)
        {
            Finish(EStatus::Cancelled);
            Actions = MoveTemp(Mapping.Actions);
            State.Bindings = MoveTemp(Mapping.Bindings);
            MappingSignature = Mapping.Signature;
            RefreshMappings(LP, PlayerInput);
            ++State.Mapping;
        }
        if (Pawn && Viewport && Viewport->Viewport && !State.Bindings.IsEmpty())
        {
            // Startup can stall on the first rendered frame after the player has spawned.
            ReadyFrames = FMath::Min(ReadyFrames + 1, 8);
        }
        else
        {
            ReadyFrames = 0;
        }
        State.bReady = ReadyFrames == 8;
        State.Pressed.Reset();
        for (int32 Index = 0; PC && Index < State.Bindings.Num(); ++Index)
        {
            if (PC->IsInputKeyDown(FKey(State.Bindings[Index].Key))) { State.Pressed.Add(Index); }
        }
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
        TSet<int32> Changed;
        while (Transitions.IsValidIndex(Next) && Transitions[Next].AtMs == At)
        {
            const auto& Step = Transitions[Next];
            if (Changed.Contains(Step.BindingIndex)) { break; }
            if (!DownDevice.IsValid() || (Step.bDown && State.Pressed.Contains(Step.BindingIndex)))
            {
                Finish(EStatus::Cancelled);
                return;
            }
            if (Step.bDown) { State.Held.Add(Step.BindingIndex); }
            if (!Send(Step.BindingIndex, Step.bDown))
            {
                Finish(EStatus::Cancelled);
                return;
            }
            if (!Step.bDown) { State.Held.Remove(Step.BindingIndex); }
            ReportedActions.Remove(Step.BindingIndex);
            Record(Step.BindingIndex, Step.bDown);
            Changed.Add(Step.BindingIndex);
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
        for (int32 Index = 0; Index < Actions.Num(); ++Index)
        {
            if (!State.Held.Contains(Index) || ReportedActions.Contains(Index)) { continue; }
            const auto* Action = Actions[Index].Get();
            const auto* Instance = Action ? PlayerInput->FindActionInstanceData(Action) : nullptr;
            if (Instance && Instance->GetTriggerEvent() == ETriggerEvent::Triggered)
            {
                Record(Index, true, ELayer::Action);
                ReportedActions.Add(Index);
                if (State.Bindings[Index].Action == JumpName && State.bGrounded)
                {
                    JumpStart = State.Position.Z;
                    JumpIndex = Index;
                }
            }
        }
        const auto* Pawn = TargetPawn.Get();
        if (JumpIndex != INDEX_NONE && Pawn && Pawn->GetActorLocation().Z > JumpStart + 0.5 && Pawn->GetVelocity().Z > 0.0)
        {
            Record(JumpIndex, true, ELayer::Pawn);
            JumpIndex = INDEX_NONE;
        }
    }

private:
    static bool IsDigitalInput(const FKey& Key)
    {
        return Key.IsValid() && Key.IsBindableInBlueprints() && !Key.IsGamepadKey()
            && !Key.IsMouseButton() && !Key.IsAnalog() && !Key.IsTouch()
            && Key != EKeys::AnyKey && Key != EKeys::F11;
    }

    inline static const FName JumpName{TEXT("IA_Jump")};

    struct FAllowedActions
    {
        TArray<FBinding> Bindings;
        TArray<TWeakObjectPtr<const UInputAction>> Actions;
        uint32 Signature = 0;
    };

    FAllowedActions FindAllowed(const UEnhancedPlayerInput* Input) const
    {
        FAllowedActions Result;
        if (!Input) { return Result; }

        TMap<FName, const FEnhancedActionKeyMapping*> ByAction;
        TArray<const FEnhancedActionKeyMapping*> Eligible;
        TMap<FName, const UInputAction*> ByKey;
        for (const auto& Mapping : Input->GetEnhancedActionMappingsView())
        {
            if (!Mapping.Action) { continue; }
            const auto Key = Mapping.Key.GetFName();
            const auto Action = Mapping.Action->GetFName();
            Result.Signature = HashCombine(Result.Signature, GetTypeHash(Key));
            Result.Signature = HashCombine(Result.Signature, PointerHash(Mapping.Action.Get()));
            for (const auto& Trigger : Mapping.Triggers) { Result.Signature = HashCombine(Result.Signature, PointerHash(Trigger.Get())); }
            for (const auto& Modifier : Mapping.Modifiers) { Result.Signature = HashCombine(Result.Signature, PointerHash(Modifier.Get())); }
            if (bAutoDiscoverKeys)
            {
                if (!IsDigitalInput(Mapping.Key) || Input->DebugExecBindings.ContainsByPredicate(
                    [&Mapping](const FKeyBind& Binding) { return Binding.Key == Mapping.Key; })) { continue; }
            }
            else if (!ApprovedKeys.Contains(Mapping.Key)) { continue; }

            Eligible.Add(&Mapping);
            if (auto** Existing = ByAction.Find(Action))
            {
                if (*Existing && ((*Existing)->Action != Mapping.Action
                    || (!bAutoDiscoverKeys && (*Existing)->Key != Mapping.Key)))
                {
                    *Existing = nullptr;
                }
            }
            else { ByAction.Add(Action, &Mapping); }

            if (auto** Existing = ByKey.Find(Key))
            {
                if (*Existing != Mapping.Action.Get()) { *Existing = nullptr; }
            }
            else { ByKey.Add(Key, Mapping.Action.Get()); }
        }

        if (bAutoDiscoverKeys)
        {
            TSet<FName> AddedKeys;
            for (const auto* Mapping : Eligible)
            {
                const auto Key = Mapping->Key.GetFName();
                const auto Action = Mapping->Action->GetFName();
                const auto* Canonical = ByAction.FindRef(Action);
                if (!Canonical || ByKey.FindRef(Key) != Mapping->Action.Get() || AddedKeys.Contains(Key)) { continue; }
                AddedKeys.Add(Key);
                Result.Bindings.Add({Key, Action});
                Result.Actions.Add(Mapping->Action.Get());
            }
            return Result;
        }

        TArray<FName> Names;
        ByAction.GetKeys(Names);
        Names.Sort(FNameLexicalLess());
        for (const auto Name : Names)
        {
            const auto* Mapping = ByAction[Name];
            if (!Mapping || ByKey.FindRef(Mapping->Key.GetFName()) != Mapping->Action.Get()) { continue; }
            Result.Bindings.Add({Mapping->Key.GetFName(), Name});
            Result.Actions.Add(Mapping->Action.Get());
        }
        return Result;
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

    bool Send(int32 Index, bool bPress)
    {
        auto* Viewport = DownViewport.Get();
        auto* PC = TargetController.Get();
        const auto Event = FInputKeyEventArgs::CreateSimulated(FKey(State.Bindings[Index].Key),
            bPress ? IE_Pressed : IE_Released, bPress ? 1.0f : 0.0f,
            -1, DownDevice, false, Viewport ? Viewport->Viewport : nullptr);
        if (Viewport && Viewport->Viewport && PC && TargetWorld.IsValid() && PC == TargetWorld->GetFirstPlayerController()
            && Viewport->InputKey(Event)) { return true; }
        return !bPress && PC && PC->InputKey(Event);
    }

    void Finish(EStatus Status)
    {
        if (State.Status != EStatus::Running) { return; }
        for (int32 Index = 0; Index < State.Bindings.Num(); ++Index)
        {
            if (State.Held.Contains(Index))
            {
                Send(Index, false);
                Record(Index, false);
            }
        }
        State.Held.Reset();
        Transitions.Reset();
        ReportedActions.Reset();
        JumpIndex = INDEX_NONE;
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

    void Record(int32 BindingIndex, bool bDown, ELayer Layer = ELayer::Key)
    {
        const uint64 Index = State.EventSeq++;
        const auto& Binding = State.Bindings[BindingIndex];
        Events[Index % EventLimit] = {State.EventSeq, Request, Binding.Key, Binding.Action, bDown, Layer};
    }

    std::mutex Mutex;
    FState State;
    TSet<FKey> ApprovedKeys;
    bool bAutoDiscoverKeys = false;
    TArray<FMappingInfo> EffectiveMappings;
    TMap<FString, int32> ActiveContexts;
    uint32 MappingSignature = 0;
    std::array<FEdge, EventLimit> Events;
    FGuid Request;
    TWeakObjectPtr<UWorld> TargetWorld;
    TWeakObjectPtr<APlayerController> TargetController;
    TWeakObjectPtr<APawn> TargetPawn;
    TArray<TWeakObjectPtr<const UInputAction>> Actions;
    TWeakObjectPtr<UGameViewportClient> DownViewport;
    FInputDeviceId DownDevice = INPUTDEVICEID_NONE;
    TArray<FTransition> Transitions;
    int32 Next = 0, Duration = 0;
    int32 ReadyFrames = 0;
    TSet<int32> ReportedActions;
    double NextAt = 0.0, DeadlineAt = 0.0, LeaseUntil = 0.0, LastWorldTick = 0.0, JumpStart = 0.0;
    bool bCancel = false;
    int32 JumpIndex = INDEX_NONE;
};
