using System.Security.Cryptography;
using System.Text.Json;

namespace DistributedRayTrace.Contracts.Models;

/// <summary>One bounded v5 RGB24 transfer inside the ordered, owned IPC stream.</summary>
public sealed class RoomFrameAssembler
{
    public const int ChunkBytes=49152;
    private readonly Guid _run;
    private readonly string _digest;
    private readonly int _width,_height;
    private readonly HashSet<Guid> _completed=[];
    private string? _manifest;
    private byte[]? _buffer;
    private int _offset,_index;
    private long _next;
    public bool IsActive=>_buffer is not null;
    public RoomFrameAssembler(Guid run,string digest,int width,int height)
    {
        Require(run!=Guid.Empty && Hash(digest) && (width,height) is (160,90) or (640,360),"Invalid room frame authority.");
        _run=run;_digest=digest;_width=width;_height=height;
    }
    public (string Purpose,byte[] Rgb)? Accept(JsonElement e)
    {
        Exact(e,"type protocolVersion runId sequence transfer");
        Require(e.GetProperty("type").GetString()=="roomFrame" && e.GetProperty("protocolVersion").GetInt32()==5
            && e.GetProperty("runId").GetString()==_run.ToString("D"),"Foreign frame session.");
        return Accept(e.GetProperty("protocolVersion").GetInt32(),Guid.Parse(e.GetProperty("runId").GetString()!),
            e.GetProperty("sequence").GetInt64(),e.GetProperty("transfer"));
    }
    public (string Purpose,byte[] Rgb)? Accept(int protocolVersion,Guid run,long sequence,JsonElement d)
    {
        Require(protocolVersion==5 && run==_run && sequence>0,"Foreign frame authority/sequence.");
        var stage=d.GetProperty("stage").GetString();
        const string common="schema frameId workloadDigest width height pixelFormat contentLength chunkCount sha256 purpose";
        Exact(d,common+" stage"+(stage=="chunk"?" index offset data":""));
        var idText=d.GetProperty("frameId").GetString();
        Require(Guid.TryParseExact(idText,"D",out var id) && id!=Guid.Empty && id.ToString("D")==idText,"Invalid frame identity.");
        var length=d.GetProperty("contentLength").GetInt32();var count=d.GetProperty("chunkCount").GetInt32();
        var hash=d.GetProperty("sha256").GetString();var purpose=d.GetProperty("purpose").GetString();
        Require(d.GetProperty("schema").GetString()=="drt.room_rgb24_chunks.v1"
            && d.GetProperty("workloadDigest").GetString()==_digest && d.GetProperty("pixelFormat").GetString()=="rgb24"
            && d.GetProperty("width").GetInt32()==_width && d.GetProperty("height").GetInt32()==_height
            && length==_width*_height*3 && count==(length+ChunkBytes-1)/ChunkBytes && Hash(hash)
            && purpose is "preview" or "final","Frame workload/format/length differs.");
        // Build a field-ordered fingerprint independent of peer JSON property order.
        var manifest=string.Join("|",common.Split(' ').Select(k=>d.GetProperty(k).GetRawText()));
        if(stage=="begin")
        {
            Require(_buffer is null && !_completed.Contains(id) && _completed.Count<21,"Duplicate/excess/overlapping frame.");
            _manifest=manifest;_buffer=new byte[length];_offset=_index=0;_next=sequence+1;return null;
        }
        Require(_buffer is not null && manifest==_manifest && sequence==_next,"Missing/reordered/foreign chunk.");
        if(stage=="chunk")
        {
            Require(d.GetProperty("index").GetInt32()==_index && d.GetProperty("offset").GetInt32()==_offset,"Invalid chunk interval.");
            var text=d.GetProperty("data").GetString();Require(text is not null && text.Length<=65536,"Chunk exceeds bound.");
            var raw=Convert.FromBase64String(text!);
            Require(raw.Length==Math.Min(ChunkBytes,length-_offset) && raw.Length>0 && Convert.ToBase64String(raw)==text,"Noncanonical chunk size.");
            raw.CopyTo(_buffer!,_offset);_offset+=raw.Length;_index++;_next++;return null;
        }
        Require(stage=="end" && _offset==length && _index==count
            && Convert.ToHexString(SHA256.HashData(_buffer!)).ToLowerInvariant()==hash,"Incomplete/corrupt room frame.");
        var result=_buffer!;_completed.Add(id);_buffer=null;_manifest=null;return (purpose!,result);
    }
    private static bool Hash(string? s)=>s is {Length:64} && s.All(c=>c is >= '0' and <= '9' or >= 'a' and <= 'f');
    private static void Require(bool value,string message) {if(!value)throw new ArgumentException(message);}
    private static void Exact(JsonElement v,string fields)
    {
        Require(v.ValueKind==JsonValueKind.Object && v.EnumerateObject().Select(p=>p.Name).Order()
            .SequenceEqual(fields.Split(' ').Order()),"Exact frame fields required.");
    }
}
