// Protocol v2: one compilation context on stdin, structured observations on stdout.
#include "clang/AST/ASTContext.h"
#include "clang/AST/ParentMapContext.h"
#include "clang/AST/RawCommentList.h"
#include "clang/AST/RecursiveASTVisitor.h"
#include "clang/Basic/Version.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Frontend/FrontendActions.h"
#include "clang/Index/USRGeneration.h"
#include "clang/Lex/Lexer.h"
#include "clang/Tooling/JSONCompilationDatabase.h"
#include "clang/Tooling/Tooling.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/SHA256.h"
#include "llvm/Support/SHA1.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/ADT/StringExtras.h"
#include <map>
#include <set>

using namespace clang;
using llvm::json::Object;
using llvm::json::Array;

static void emit(Object record) { llvm::outs() << llvm::json::Value(std::move(record)) << '\n'; }
static std::string usr(const Decl *D) {
  llvm::SmallString<128> value;
  if (!D || index::generateUSRForDecl(D->getCanonicalDecl(), value)) return "";
  return std::string(value);
}
static std::string absolute(llvm::StringRef path) {
  llvm::SmallString<256> result(path);
  llvm::sys::fs::make_absolute(result);
  llvm::sys::path::remove_dots(result, true);
  return std::string(result);
}

struct Output {
  std::set<std::string> allowed, metadataFiles, ids;
  std::set<std::string> declarations;
  bool known(const Decl *D) {
    if (ids.empty()) return true;
    auto value = usr(D);
    auto digest = llvm::SHA1::hash(llvm::arrayRefFromStringRef(value));
    return !value.empty() && ids.count(llvm::toHex(llvm::ArrayRef<uint8_t>(digest.data(), 8)));
  }
  std::set<std::string> files;
  int errors = 0;
  std::string path(SourceLocation loc, SourceManager &SM) {
    if (loc.isInvalid()) return "";
    auto file = SM.getFileEntryRefForID(SM.getFileID(SM.getExpansionLoc(loc)));
    return file ? absolute(file->getName()) : "";
  }
  void file(FileID id, SourceManager &SM) {
    auto entry = SM.getFileEntryRefForID(id);
    if (!entry) return;
    auto name = absolute(entry->getName());
    if (!metadataFiles.count(name) || !files.insert(name).second) return;
    bool invalid = false;
    auto bytes = SM.getBufferData(id, &invalid);
    if (invalid) return;
    auto digest = llvm::SHA256::hash(llvm::arrayRefFromStringRef(bytes));
    emit(Object{{"type", "file"}, {"path", name}, {"sha256", llvm::toHex(digest, true)}});
  }
  llvm::json::Value range(SourceRange R, ASTContext &C, bool metadata = false) {
    auto &SM = C.getSourceManager();
    auto chars = Lexer::makeFileCharRange(CharSourceRange::getTokenRange(R), SM, C.getLangOpts());
    if (chars.isInvalid()) return nullptr;
    auto begin = chars.getBegin(), end = chars.getEnd();
    if (SM.getFileID(begin) != SM.getFileID(end)) return nullptr;
    auto name = path(begin, SM);
    if (!(metadata ? metadataFiles : allowed).count(name)) return nullptr;
    file(SM.getFileID(begin), SM);
    return Object{{"path", name}, {"start", int64_t(SM.getFileOffset(begin))},
                  {"end", int64_t(SM.getFileOffset(end))}};
  }
  void problem(llvm::StringRef message, SourceLocation loc, ASTContext &C, llvm::StringRef stage = "references") {
    emit(Object{{"type", "issue"}, {"path", path(loc, C.getSourceManager())}, {"message", message}, {"stage", stage}});
  }
  std::string kind(const NamedDecl *D) {
    if (isa<CXXConstructorDecl>(D)) return "constructor";
    if (isa<CXXDestructorDecl>(D)) return "destructor";
    if (isa<CXXConversionDecl>(D)) return "conversion_function";
    if (auto *M = dyn_cast<CXXMethodDecl>(D)) return M->isStatic() ? "static_method" : "instance_method";
    if (isa<FunctionDecl>(D)) return "function";
    if (auto *R = dyn_cast<RecordDecl>(D)) return R->isUnion() ? "union" : R->isStruct() ? "struct" : "class";
    if (isa<NamespaceDecl>(D)) return "namespace";
    if (isa<EnumDecl>(D)) return "enum";
    if (isa<EnumConstantDecl>(D)) return "enum_constant";
    if (isa<FieldDecl>(D)) return "field";
    if (isa<TypedefNameDecl>(D)) return "type_alias";
    if (auto *V = dyn_cast<VarDecl>(D)) return V->isStaticDataMember() ? "static_property" : "variable";
    return "";
  }
  void declaration(const NamedDecl *input, ASTContext &C) {
    if (!input || !known(input)) return;
    auto *D = dyn_cast<NamedDecl>(input->getCanonicalDecl());
    if (!D || kind(D).empty()) return;
    auto id = usr(D);
    if (id.empty() || declarations.count(id)) return;
    auto &SM = C.getSourceManager();
    auto evidence = SM.getExpansionLoc(D->getLocation());
    auto name = path(evidence, SM);
    if (!metadataFiles.count(name)) return;
    declarations.insert(id);
    file(SM.getFileID(evidence), SM);
    const NamedDecl *parent = nullptr;
    const DeclContext *context = D->getDeclContext();
    while (context && !context->isTranslationUnit()) {
      parent = dyn_cast<NamedDecl>(Decl::castFromDeclContext(context));
      if (parent) break;
      context = context->getParent();
    }
    if (parent) declaration(parent, C);
    PrintingPolicy policy(C.getLangOpts());
    policy.SuppressScope = false;
    std::string signature, returnType, eligibility = "not_applicable", arguments, comment;
    const NamedDecl *definition = nullptr;
    if (auto *F = dyn_cast<FunctionDecl>(D)) {
      auto *defined = F->getDefinition();
      definition = defined;
      bool deleted = false, defaulted = false;
      for (auto *R : F->redecls()) { deleted |= R->isDeleted(); defaulted |= R->isDefaulted(); }
      eligibility = deleted ? "deleted" : defaulted ? "defaulted" :
                    F->isImplicit() ? "implicit" : !defined ? "declaration_only" : "written";
      llvm::raw_string_ostream stream(signature);
      stream << "(";
      for (unsigned i = 0; i < F->getNumParams(); ++i) {
        if (i) stream << ", ";
        auto *P = F->getParamDecl(i);
        P->getType().print(stream, policy, P->getName());
      }
      if (F->isVariadic()) stream << (F->getNumParams() ? ", ..." : "...");
      stream << ")";
      if (auto *M = dyn_cast<CXXMethodDecl>(F)) {
        if (M->isConst()) stream << " const";
        if (M->isVolatile()) stream << " volatile";
        if (M->getRefQualifier() == RQ_LValue) stream << " &";
        if (M->getRefQualifier() == RQ_RValue) stream << " &&";
      }
      if (auto *T = F->getType()->getAs<FunctionProtoType>()) {
        if (auto *E = T->getNoexceptExpr()) { stream << " noexcept("; E->printPretty(stream, nullptr, policy); stream << ")"; }
        else if (T->getExceptionSpecType() == EST_BasicNoexcept || T->getExceptionSpecType() == EST_NoThrow) stream << " noexcept";
        else if (T->getExceptionSpecType() == EST_DynamicNone) stream << " throw()";
        else if (T->getExceptionSpecType() == EST_Dynamic) {
          stream << " throw(";
          for (unsigned i = 0; i < T->getNumExceptions(); ++i) { if (i) stream << ", "; T->getExceptionType(i).print(stream, policy); }
          stream << ")";
        }
      }
      if (!isa<CXXConstructorDecl, CXXDestructorDecl>(F)) returnType = F->getReturnType().getAsString(policy);
      if (auto *args = F->getTemplateSpecializationArgs()) {
        llvm::raw_string_ostream a(arguments); a << "<";
        for (unsigned i = 0; i < args->size(); ++i) { if (i) a << ", "; args->get(i).print(policy, a, true); }
        a << ">";
      }
    } else if (auto *T = dyn_cast<TagDecl>(D)) definition = T->getDefinition();
    else if (auto *V = dyn_cast<VarDecl>(D)) definition = V->getDefinition();
    if (auto *T = dyn_cast<ClassTemplateSpecializationDecl>(D)) {
      llvm::raw_string_ostream a(arguments); a << "<";
      for (unsigned i = 0; i < T->getTemplateArgs().size(); ++i) { if (i) a << ", "; T->getTemplateArgs()[i].print(policy, a, true); }
      a << ">";
    }
    if (auto *raw = C.getRawCommentForDeclNoCache(D)) comment = raw->getRawText(SM).str();
    auto safeName = [&](const NamedDecl *N) -> llvm::json::Value {
      if (!N || N->getLocation().isMacroID()) return nullptr;
      return range(SourceRange(N->getLocation()), C, true);
    };
    std::string scope = parent ? parent->getQualifiedNameAsString() + "::" : "";
    if (parent && scope == "::") scope = "(anonymous)::";
    emit(Object{{"type", "declaration"}, {"usr", id}, {"name", D->getNameAsString()},
         {"kind", kind(D)}, {"language", C.getLangOpts().CPlusPlus ? "Cpp" : "C"},
         {"parent_usr", parent ? usr(parent) : ""}, {"scope", scope}, {"path", name},
         {"declaration", safeName(D)}, {"definition", safeName(definition)},
         {"signature", signature}, {"return_type", returnType}, {"template_arguments", arguments},
         {"documentation", comment}, {"eligibility", eligibility}});
  }

};

// Written constructs, not desugared AST operations. Lambda bodies count in their
// enclosing body's metrics. Local class methods are independent bodies.
class Metrics : public RecursiveASTVisitor<Metrics> {
  ASTContext &C;
  std::map<std::string, int64_t> values;
  int depth = 0;
  bool nesting(Stmt *S) {
    return isa<IfStmt, SwitchStmt, ForStmt, CXXForRangeStmt, WhileStmt, DoStmt, CXXTryStmt>(S);
  }
  bool expressionStatement(Expr *E) {
    auto parents = C.getParents(*E);
    if (parents.empty()) return false;
    const Stmt *P = parents[0].get<Stmt>();
    if (!P) return false;
    if (isa<CompoundStmt>(P)) return true;
    if (auto *I = dyn_cast<IfStmt>(P)) return I->getThen() == E || I->getElse() == E;
    if (auto *F = dyn_cast<ForStmt>(P)) return F->getBody() == E || F->getInit() == E;
    if (auto *W = dyn_cast<WhileStmt>(P)) return W->getBody() == E;
    if (auto *D = dyn_cast<DoStmt>(P)) return D->getBody() == E;
    if (auto *F = dyn_cast<CXXForRangeStmt>(P)) return F->getBody() == E;
    if (auto *L = dyn_cast<LabelStmt>(P)) return L->getSubStmt() == E;
    if (auto *S = dyn_cast<SwitchCase>(P)) return S->getSubStmt() == E;
    return false;
  }
public:
  Metrics(ASTContext &C, unsigned parameters) : C(C) {
    for (auto name : {"statement_count", "if_count", "switch_count", "case_count", "loop_count",
         "max_nesting_depth", "return_count", "goto_count", "break_count", "continue_count",
         "try_count", "catch_count", "throw_count", "local_variable_count", "lambda_count"}) values[name] = 0;
    values["parameter_count"] = parameters;
    values["cyclomatic_complexity"] = 1;
  }
  bool TraverseCXXRecordDecl(CXXRecordDecl *) { return true; }
  bool TraverseStmt(Stmt *S, DataRecursionQueue * = nullptr) {
    if (!S) return true;
    bool nested = nesting(S);
    if (nested) values["max_nesting_depth"] = std::max(values["max_nesting_depth"], int64_t(++depth));
    bool result = RecursiveASTVisitor::TraverseStmt(S);
    if (nested) --depth;
    return result;
  }
  bool TraverseCXXForRangeStmt(CXXForRangeStmt *S) {
    WalkUpFromCXXForRangeStmt(S);
    TraverseStmt(S->getInit());
    TraverseDecl(S->getLoopVariable());
    TraverseStmt(S->getRangeInit());
    TraverseStmt(S->getBody());
    return true;
  }
  bool VisitStmt(Stmt *S) {
    if ((!isa<Expr, CompoundStmt>(S)) || (isa<Expr>(S) && expressionStatement(cast<Expr>(S))))
      ++values["statement_count"];
    if (isa<IfStmt>(S)) { ++values["if_count"]; ++values["cyclomatic_complexity"]; }
    if (isa<SwitchStmt>(S)) ++values["switch_count"];
    if (isa<CaseStmt>(S)) { ++values["case_count"]; ++values["cyclomatic_complexity"]; }
    if (isa<ForStmt, CXXForRangeStmt, WhileStmt, DoStmt>(S)) { ++values["loop_count"]; ++values["cyclomatic_complexity"]; }
    if (isa<ReturnStmt>(S)) ++values["return_count"];
    if (isa<GotoStmt, IndirectGotoStmt>(S)) ++values["goto_count"];
    if (isa<BreakStmt>(S)) ++values["break_count"];
    if (isa<ContinueStmt>(S)) ++values["continue_count"];
    if (isa<CXXTryStmt>(S)) ++values["try_count"];
    if (isa<CXXCatchStmt>(S)) { ++values["catch_count"]; ++values["cyclomatic_complexity"]; }
    if (isa<CXXThrowExpr>(S)) ++values["throw_count"];
    if (isa<LambdaExpr>(S)) ++values["lambda_count"];
    if (isa<ConditionalOperator, BinaryConditionalOperator>(S)) ++values["cyclomatic_complexity"];
    if (auto *B = dyn_cast<BinaryOperator>(S))
      if (B->isLogicalOp()) ++values["cyclomatic_complexity"];
    return true;
  }
  bool VisitVarDecl(VarDecl *D) {
    if (!D->isImplicit() && !isa<ParmVarDecl>(D) && D->isLocalVarDecl()) ++values["local_variable_count"];
    return true;
  }
  Object result() { Object result; for (auto &[key, value] : values) result[key] = value; return result; }
};

class Calls : public RecursiveASTVisitor<Calls> {
  ASTContext &C;
  Output &O;
  const FunctionDecl *owner;
public:
  Calls(ASTContext &C, Output &O, const FunctionDecl *owner) : C(C), O(O), owner(owner) {}
  bool TraverseCXXRecordDecl(CXXRecordDecl *) { return true; }
  bool TraverseTypeLoc(TypeLoc) { return true; }
  bool TraverseUnaryExprOrTypeTraitExpr(UnaryExprOrTypeTraitExpr *) { return true; }
  bool TraverseCXXNoexceptExpr(CXXNoexceptExpr *) { return true; }
  bool TraverseCXXTypeidExpr(CXXTypeidExpr *E) {
    O.problem("typeid operand call analysis deferred", E->getExprLoc(), C); return true;
  }
  bool TraverseRequiresExpr(RequiresExpr *) { return true; }
  bool TraverseCXXDefaultArgExpr(CXXDefaultArgExpr *) { return true; }
  bool TraverseCXXDefaultInitExpr(CXXDefaultInitExpr *) { return true; }
  bool TraverseCXXForRangeStmt(CXXForRangeStmt *S) {
    TraverseStmt(S->getInit());
    TraverseStmt(S->getRangeInit());
    TraverseStmt(S->getBody());
    return true;
  }
  bool TraverseIfStmt(IfStmt *S) {
    if (!S->isConstexpr()) return RecursiveASTVisitor::TraverseIfStmt(S);
    TraverseStmt(S->getInit());
    TraverseStmt(S->getConditionVariableDeclStmt());
    TraverseStmt(S->getCond());
    auto branch = S->getNondiscardedCase(C);
    if (branch) TraverseStmt(*branch);
    else O.problem("dependent constexpr branch calls deferred", S->getIfLoc(), C);
    return true;
  }
  bool TraverseLambdaExpr(LambdaExpr *L) {
    // Capture initializers execute in the enclosing callable.
    for (auto *init : L->capture_inits()) TraverseStmt(init);
    auto *previous = owner;
    owner = L->getCallOperator();
    TraverseStmt(L->getBody());
    owner = previous;
    return true;
  }
  bool VisitCallExpr(CallExpr *E) {
    auto *target = E->getDirectCallee();
    if (!target) { O.problem("unresolved indirect or dependent call", E->getExprLoc(), C); return true; }
    if (isa<CXXConversionDecl>(target)) {
      O.problem("conversion calls deferred", E->getExprLoc(), C); return true;
    }
    O.declaration(target, C);
    O.declaration(owner, C);
    SourceLocation token;
    if (auto *op = dyn_cast<CXXOperatorCallExpr>(E)) token = op->getOperatorLoc();
    else {
      auto *callee = E->getCallee()->IgnoreParenImpCasts();
      if (auto *ref = dyn_cast<DeclRefExpr>(callee)) token = ref->getLocation();
      if (auto *member = dyn_cast<MemberExpr>(callee))
        if (!member->getMemberLoc().isInvalid()) token = member->getMemberLoc();
    }
    if (token.isMacroID()) {
      O.problem("macro-generated call location deferred", E->getExprLoc(), C); return true;
    }
    auto location = O.range(SourceRange(token), C);
    if (location.kind() == llvm::json::Value::Null) {
      O.problem("call has no unambiguous spelled callee range", E->getExprLoc(), C); return true;
    }
    emit(Object{{"type", "call"}, {"target_usr", usr(target)}, {"container_usr", usr(owner)},
          {"location", std::move(location)}});
    return true;
  }
};

class Visitor : public RecursiveASTVisitor<Visitor> {
  ASTContext &C;
  Output &O;
  std::set<const FunctionDecl *> seen;
public:
  Visitor(ASTContext &C, Output &O) : C(C), O(O) {}
  bool VisitNamedDecl(NamedDecl *D) {
    if (O.allowed.count(O.path(D->getLocation(), C.getSourceManager()))) O.declaration(D, C);
    return true;
  }
  bool VisitCXXRecordDecl(CXXRecordDecl *D) {
    if (!D->isCompleteDefinition() || !O.known(D) ||
        !O.allowed.count(O.path(D->getLocation(), C.getSourceManager()))) return true;
    for (const auto &base : D->bases()) {
      auto *B = base.getType()->getAsCXXRecordDecl();
      bool existingEdgeOnly = false;
      if (!B && base.getType()->isDependentType()) {
        // A named template pattern identifies an imported edge, but does not
        // establish which specialization will be selected at instantiation.
        auto *type = base.getType()->getAs<TemplateSpecializationType>();
        auto *templ = type && !type->isTypeAlias()
            ? dyn_cast_or_null<ClassTemplateDecl>(type->getTemplateName().getAsTemplateDecl()) : nullptr;
        if (templ) {
          B = templ->getTemplatedDecl();
          existingEdgeOnly = true;
        }
      }
      if (!B) { O.problem("dependent base unresolved", D->getLocation(), C, "symbols"); continue; }
      O.declaration(B, C);
      auto access = base.getAccessSpecifier();
      emit(Object{{"type", "inheritance"}, {"base_usr", usr(B)}, {"derived_usr", usr(D)},
                  {"access", access == AS_public ? "public" : access == AS_protected ? "protected" : "private"},
                  {"virtual", base.isVirtual()}, {"existing_edge_only", existingEdgeOnly},
                  {"path", O.path(D->getLocation(), C.getSourceManager())}});
    }
    return true;
  }
  bool VisitFunctionDecl(FunctionDecl *D) {
    if (auto *M = dyn_cast<CXXMethodDecl>(D)) {
      if (O.known(D) && O.allowed.count(O.path(D->getLocation(), C.getSourceManager())))
        for (auto *base : M->overridden_methods()) {
          O.declaration(base, C);
          emit(Object{{"type", "override"}, {"base_usr", usr(base)}, {"derived_usr", usr(D)},
                      {"path", O.path(D->getLocation(), C.getSourceManager())}});
        }
    }
    if (D->isImplicit() || D->isDefaulted() || !D->doesThisDeclarationHaveABody() || !seen.insert(D).second) return true;
    auto *body = D->getBody();
    if (!body || !O.allowed.count(O.path(D->getLocation(), C.getSourceManager()))) return true;
    if (isa<CoroutineBodyStmt>(body)) {
      O.problem("coroutine body analysis deferred", D->getLocation(), C, "metrics"); return true;
    }
    if (body->getBeginLoc().isMacroID() || body->getEndLoc().isMacroID()) {
      O.problem("macro-generated body extent deferred", D->getLocation(), C, "metrics"); return true;
    }
    auto range = O.range(body->getSourceRange(), C);
    if (range.kind() == llvm::json::Value::Null) {
      O.problem("body has no unambiguous contiguous source range", D->getLocation(), C, "metrics"); return true;
    }
    Metrics metrics(C, D->getNumParams());
    metrics.TraverseStmt(body);
    emit(Object{{"type", "body"}, {"usr", usr(D)}, {"name", D->getQualifiedNameAsString()},
          {"body", std::move(range)}, {"definition", O.range(D->getSourceRange(), C)},
          {"metrics", metrics.result()}});
    Calls calls(C, O, D);
    calls.TraverseStmt(body);
    return true;
  }
};
class Consumer : public ASTConsumer {
  Output &O;
public:
  explicit Consumer(Output &O) : O(O) {}
  void HandleTranslationUnit(ASTContext &C) override {
    Visitor(C, O).TraverseDecl(C.getTranslationUnitDecl());
    auto &SM = C.getSourceManager();
    for (auto it = SM.fileinfo_begin(); it != SM.fileinfo_end(); ++it) {
      auto id = SM.translateFile(it->first);
      if (id.isValid() && O.allowed.count(O.path(SM.getLocForStartOfFile(id), SM))) O.file(id, SM);
    }
  }
};
class Action : public ASTFrontendAction {
  Output &O;
public:
  explicit Action(Output &O) : O(O) {}
  std::unique_ptr<ASTConsumer> CreateASTConsumer(CompilerInstance &, llvm::StringRef) override {
    return std::make_unique<Consumer>(O);
  }
};
class Factory : public tooling::FrontendActionFactory {
  Output &O;
public:
  explicit Factory(Output &O) : O(O) {}
  std::unique_ptr<FrontendAction> create() override { return std::make_unique<Action>(O); }
};
class Diagnostics : public DiagnosticConsumer {
  Output &O;
public:
  explicit Diagnostics(Output &O) : O(O) {}
  void HandleDiagnostic(DiagnosticsEngine::Level level, const Diagnostic &D) override {
    DiagnosticConsumer::HandleDiagnostic(level, D);
    if (level >= DiagnosticsEngine::Error) ++O.errors;
    llvm::SmallString<256> message;
    D.FormatDiagnostic(message);
    std::string path;
    if (D.hasSourceManager()) path = O.path(D.getLocation(), D.getSourceManager());
    emit(Object{{"type", "diagnostic"}, {"path", path}, {"error", level >= DiagnosticsEngine::Error},
          {"message", std::string(message)}});
  }
};
int main(int argc, char **argv) {
  if (argc == 2 && llvm::StringRef(argv[1]) == "--version") {
    emit(Object{{"type", "version"}, {"protocol", 2}, {"llvm_major", CLANG_VERSION_MAJOR}}); return 0;
  }
  auto input = llvm::MemoryBuffer::getSTDIN();
  if (!input) return 2;
  auto parsed = llvm::json::parse((*input)->getBuffer());
  if (!parsed) { llvm::consumeError(parsed.takeError()); return 2; }
  auto *request = parsed->getAsObject();
  if (!request || request->getInteger("protocol") != 2) return 2;
  auto *command = request->getObject("command");
  auto *paths = request->getArray("files");
  if (!command || !paths || !command->getString("file")) return 2;
  Output output;
  for (auto &path : *paths) { if (!path.getAsString()) return 2; output.allowed.insert(absolute(*path.getAsString())); }
  output.metadataFiles = output.allowed;
  if (auto *extra = request->getArray("metadata_files"))
    for (auto &path : *extra) { if (!path.getAsString()) return 2; output.metadataFiles.insert(absolute(*path.getAsString())); }
  if (auto *ids = request->getArray("ids"))
    for (auto &id : *ids) { if (!id.getAsString()) return 2; output.ids.insert(id.getAsString()->str()); }
  std::string database;
  llvm::raw_string_ostream stream(database);
  stream << llvm::json::Value(Array{llvm::json::Value(Object(*command))});
  std::string error;
  auto DB = tooling::JSONCompilationDatabase::loadFromBuffer(database, error, tooling::JSONCommandLineSyntax::AutoDetect);
  if (!DB) { llvm::errs() << error; return 2; }
  emit(Object{{"type", "version"}, {"protocol", 2}, {"llvm_major", CLANG_VERSION_MAJOR}});
  tooling::ClangTool tool(*DB, {std::string(*command->getString("file"))});
  Diagnostics diagnostics(output);
  tool.setDiagnosticConsumer(&diagnostics);
  Factory factory(output);
  int status = tool.run(&factory);
  emit(Object{{"type", "complete"}, {"ok", status == 0 && output.errors == 0}});
  return 0;
}
