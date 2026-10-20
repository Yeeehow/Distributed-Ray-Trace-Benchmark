namespace DistributedRayTrace.Contracts.Enums;

public enum RenderMode { LocalAllCore = 1, DistributedAllCore = 2, CpuSerialPreview = 3 }
public enum RenderState { Idle, Preparing, Rendering, Cancelling, Completed, Cancelled, Failed }
public enum ContributorType { SoftwareWorker = 1, Node = 2 }
public enum RenderOrigin { Mock = 1, Real = 2 }
public enum ImageEncoding { Png = 1 }
