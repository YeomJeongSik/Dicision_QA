using UnrealBuildTool;

public class BackgroundInputProbe : ModuleRules
{
    public BackgroundInputProbe(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        PrivateDependencyModuleNames.AddRange(new[] {
            "Core", "CoreUObject", "Engine", "InputCore", "EnhancedInput", "ApplicationCore", "SlateCore"
        });
    }
}
